#!/usr/bin/env python3
"""Build self-contained illustrated tutorials from local media packages."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
TUTORIALS = ROOT / "tutorials"
LOCAL_BIN = ROOT / ".tools" / "bin"
if LOCAL_BIN.is_dir():
    os.environ["PATH"] = str(LOCAL_BIN) + os.pathsep + os.environ.get("PATH", "")

MEDIA_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".m4a", ".mp3", ".wav", ".flac"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
SUBTITLE_SUFFIXES = {".srt", ".vtt"}
ROLES = ("video", "audio", "cover", "subtitle")
IMAGE_LINK = re.compile(r"!\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)")
CODEX_INIT_ERROR = "failed to initialize in-process app-server client: Operation not permitted"
REFINE_SCHEMA_VERSION = 2
REFINE_PROMPT_VERSION = "refine-batched-v3"
MAX_IMAGES_PER_BATCH_DEFAULT = 12
MAX_TRANSCRIPT_CHARS_PER_BATCH_DEFAULT = 12000
CONTEXT_OVERLAP_SECONDS_DEFAULT = 6.0
BATCH_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "frames": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"image": {"type": "string"}, "observation": {"type": "string"},
                           "tutorial_value": {"type": "string"}},
            "required": ["image", "observation", "tutorial_value"]}},
        "notes_md": {"type": "string"},
        "carry_forward": {"type": "string"},
    },
    "required": ["frames", "notes_md", "carry_forward"],
}


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_sha256(value):
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def slugify(title):
    text = unicodedata.normalize("NFKC", title).strip().lower()
    text = re.sub(r"[^\w-]+", "-", text, flags=re.UNICODE).strip("-_\n")
    if not text or text in {".", ".."}:
        raise ValueError("标题不能生成有效目录名")
    return text[:80].rstrip("-_")


def package_path(reference):
    candidate = Path(reference).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    if candidate.is_absolute() or "/" in reference or reference in {".", ".."}:
        raise ValueError(f"教程目录不存在：{reference}")
    candidate = TUTORIALS / reference
    if not candidate.is_dir():
        raise ValueError(f"教程目录不存在：{candidate}")
    return candidate.resolve()


def read_contract(package):
    contract_path = package / "input" / "material.json"
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取素材合同 {contract_path}：{error}") from error
    if not isinstance(contract, dict) or contract.get("schema_version") != 1:
        raise ValueError("material.json 的 schema_version 必须为 1")
    if not isinstance(contract.get("title"), str) or not contract["title"].strip():
        raise ValueError("material.json 必须包含非空 title")
    assets = contract.get("assets")
    if not isinstance(assets, dict) or set(assets) != set(ROLES):
        raise ValueError("material.json 的 assets 必须包含 video、audio、cover、subtitle")
    return contract


def asset_path(package, role, value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{role} 路径必须是非空字符串或 null")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise ValueError(f"{role} 必须使用 input/ 内的相对路径：{value}")
    base = package / "input"
    path = base / relative
    if path.is_symlink() or any(p.is_symlink() for p in list(path.parents)[:len(relative.parts)]):
        raise ValueError(f"{role} 不允许使用符号链接：{value}")
    if base.resolve() not in path.resolve().parents:
        raise ValueError(f"{role} 路径越过 input/：{value}")
    if not path.is_file():
        raise ValueError(f"{role} 文件不存在：{path}")
    return path


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    types = {stream.get("codec_type") for stream in data.get("streams", [])}
    return duration, types


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hashes(root):
    return {path.relative_to(root): hash_file(path) for path in root.rglob("*") if path.is_file()}


def extraction_digest(extracted):
    digest = hashlib.sha256()
    for relative, fingerprint in sorted(tree_hashes(extracted).items()):
        if relative == Path("extraction.json"):
            continue
        digest.update(relative.as_posix().encode("utf-8") + b"\0" + fingerprint.encode("ascii") + b"\n")
    return digest.hexdigest()


def parse_timestamp(value):
    parts = value.replace(",", ".").split(":")
    if len(parts) not in (2, 3) or "." not in parts[-1]:
        raise ValueError(f"无效字幕时间：{value}")
    seconds, millis = parts[-1].split(".", 1)
    if len(millis) != 3 or not all(part.isdigit() for part in (*parts[:-1], seconds, millis)):
        raise ValueError(f"无效字幕时间：{value}")
    hours, minutes = (int(parts[0]), int(parts[1])) if len(parts) == 3 else (0, int(parts[0]))
    if int(seconds) >= 60 or (len(parts) == 3 and minutes >= 60):
        raise ValueError(f"无效字幕时间：{value}")
    return hours * 3600 + minutes * 60 + int(seconds) + int(millis) / 1000


def check_subtitle(path, video_duration):
    content = path.read_text(encoding="utf-8-sig")
    cues = 0
    for line in content.splitlines():
        if "-->" not in line:
            continue
        parts = [part.strip().split() for part in line.split("-->", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"无效字幕时间轴：{line}")
        left, right = parts[0][0], parts[1][0]
        start, end = parse_timestamp(left), parse_timestamp(right)
        if start >= end or end > video_duration + 2:
            raise ValueError(f"字幕时间超出视频范围或顺序错误：{line}")
        cues += 1
    if not cues:
        raise ValueError("字幕缺少有效时间轴")
    return cues


def check(package, verbose=True):
    if shutil.which("ffprobe") is None:
        raise RuntimeError("缺少 ffprobe；请安装 ffmpeg")
    contract = read_contract(package)
    assets = {role: asset_path(package, role, contract["assets"][role]) for role in ROLES}
    video = assets["video"]
    if video is None:
        raise ValueError("素材合同尚未指定 video；请先运行 scan 或编辑 material.json")
    if video.suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError("video 不是受支持的媒体格式")
    video_duration, video_types = probe(video)
    if "video" not in video_types or video_duration <= 0:
        raise ValueError("video 没有可读取的画面或时长")
    if assets["audio"]:
        audio_duration, audio_types = probe(assets["audio"])
        if "audio" not in audio_types or audio_duration <= 0:
            raise ValueError("audio 没有可读取的音轨或时长")
        if abs(video_duration - audio_duration) > max(2.0, video_duration * 0.01):
            raise ValueError(f"独立音频与视频时长不匹配：{video_duration:.1f}s / {audio_duration:.1f}s")
    if assets["cover"] and assets["cover"].suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError("cover 必须是 jpg、jpeg、png 或 webp")
    subtitle = assets["subtitle"]
    if subtitle:
        if subtitle.suffix.lower() not in SUBTITLE_SUFFIXES:
            raise ValueError("subtitle 必须是 srt 或 vtt")
        check_subtitle(subtitle, video_duration)
    elif not assets["audio"] and "audio" not in video_types:
        raise ValueError("视频没有音轨，且未提供 audio 或 subtitle；无法生成有依据的教程")
    if verbose:
        print(f"校验通过：{package}\n视频时长：{video_duration:.1f}s；文字来源：{'字幕' if subtitle else '独立音频' if assets['audio'] else '视频音轨'}")
    return contract, assets


def scan(package, dry_run=False):
    contract = read_contract(package)
    input_dir = package / "input"
    candidates = {role: [] for role in ROLES}
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file() or path.name == "material.json" or path.is_symlink():
            continue
        suffix = path.suffix.lower()
        role = None
        if suffix in IMAGE_SUFFIXES:
            role = "cover"
        elif suffix in SUBTITLE_SUFFIXES:
            role = "subtitle"
        elif suffix in MEDIA_SUFFIXES:
            _, types = probe(path)
            role = "video" if "video" in types else "audio" if "audio" in types else None
        if role:
            candidates[role].append(path.relative_to(input_dir).as_posix())
    for role in ROLES:
        current = contract["assets"][role]
        if current is not None and (input_dir / current).is_file():
            continue
        if len(candidates[role]) > 1:
            raise ValueError(f"{role} 有多个候选，请在 material.json 手工指定：{', '.join(candidates[role])}")
        if candidates[role]:
            contract["assets"][role] = candidates[role][0]
        elif current is not None:
            raise ValueError(f"{role} 指向的文件不存在：{current}")
    if not dry_run:
        write_json(input_dir / "material.json", contract)
    print(json.dumps(contract["assets"], ensure_ascii=False, indent=2))
    return contract


def init(title, source=None):
    package = TUTORIALS / slugify(title)
    if package.exists():
        raise ValueError(f"目录已存在：{package}")
    if source:
        source = source.expanduser().resolve()
        if not source.is_dir():
            raise ValueError(f"资源目录不存在：{source}")
        files = [p for p in sorted(source.iterdir()) if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES | IMAGE_SUFFIXES | SUBTITLE_SUFFIXES]
        if not files:
            raise ValueError(f"资源目录没有支持的视频、音频、封面或字幕：{source}")
    TUTORIALS.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".init-", dir=TUTORIALS))
    input_dir = temporary / "input"
    input_dir.mkdir()
    (temporary / "extracted").mkdir()
    (temporary / "output").mkdir()
    (temporary / "log").mkdir()
    contract = {"schema_version": 1, "title": title, "assets": {role: None for role in ROLES}}
    write_json(input_dir / "material.json", contract)
    try:
        if source:
            for path in files:
                shutil.copy2(path, input_dir / path.name)
            scan(temporary)
        os.rename(temporary, package)
    except Exception:
        shutil.rmtree(temporary)
        raise
    print(f"教程目录：{package}")
    return package


@contextmanager
def package_lock(package):
    logs = package / "log"
    logs.mkdir(exist_ok=True)
    with (logs / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield logs


def begin_attempt(logs, action):
    attempt = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%dT%H%M%S-") + action + "-", dir=logs))
    write_json(attempt / "status.json", {"run_id": attempt.name, "action": action,
                                         "status": "running", "started_at_utc": now(),
                                         "pid": os.getpid()})
    stage = attempt / ".staging"
    stage.mkdir()
    return attempt, stage


def finish_attempt(attempt, status, error=None):
    value = json.loads((attempt / "status.json").read_text(encoding="utf-8"))
    value.update({"status": status, "finished_at_utc": now()})
    if error is not None:
        value["error"] = str(error)
    write_json(attempt / "status.json", value)
    stage = attempt / ".staging"
    if stage.exists():
        try:
            shutil.rmtree(stage)
        except OSError as cleanup_error:
            print(f"警告：临时文件未清理：{stage}；{cleanup_error}", file=sys.stderr)


def mark_stale_running_attempts(logs):
    for record in sorted(logs.glob("*/status.json")):
        try:
            value = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("status") != "running":
            continue
        value.update({"status": "failed", "finished_at_utc": now(),
                      "error": "stale running attempt recovered by next locked run"})
        write_json(record, value)


def read_record(package, directory, filename):
    target = package / directory
    record = target / filename
    if not target.exists():
        return None
    if target.is_dir() and not any(target.iterdir()):
        return None
    if not target.is_dir() or not record.is_file():
        raise ValueError(f"已有目录不完整：{target}；请检查 log/ 后使用 --overwrite")
    return json.loads(record.read_text(encoding="utf-8"))


def read_extraction(package):
    return read_record(package, "extracted", "extraction.json")


def read_output(package):
    return read_record(package, "output", "run.json")


def publish_directory(package, directory, candidate, attempt):
    target = package / directory
    backup = attempt / f".previous-{directory}"
    if target.exists():
        os.rename(target, backup)
    try:
        os.rename(candidate, target)
    except Exception:
        if backup.exists():
            os.rename(backup, target)
        raise
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError as error:
            print(f"警告：新输出已发布，但旧输出临时备份未清理：{backup}；{error}", file=sys.stderr)
    print(f"{directory}：{target}")
    return target


def recover_directory(package, logs, directory, filename):
    backups = [p for p in logs.glob(f"*/.previous-{directory}") if p.is_dir()]
    if not backups:
        return
    target = package / directory
    if not target.exists() and len(backups) == 1:
        os.rename(backups[0], target)
        print(f"已恢复中断前的目录：{target}", file=sys.stderr)
    elif target.exists() and (target / filename).is_file():
        for backup in backups:
            shutil.rmtree(backup)
    else:
        raise RuntimeError(f"检测到未完成的目录替换；请检查 log/ 中的 .previous-{directory}")


def recover_directories(package, logs):
    recover_directory(package, logs, "extracted", "extraction.json")
    recover_directory(package, logs, "output", "run.json")


def asset_hashes(assets):
    return {role: hash_file(path) if path else None for role, path in assets.items()}


def asset_fingerprints(contract, hashes):
    return {role: ({"path": contract["assets"][role], "sha256": hashes[role]} if hashes[role] else None)
            for role in ROLES}


def assert_input_stable(package, contract, assets, hashes):
    if read_contract(package) != contract or asset_hashes(assets) != hashes:
        raise RuntimeError("处理期间输入素材或素材合同发生变化；结果未发布")


def normalize_source(raw, destination):
    frames = raw / "frames"
    images = [path for path in frames.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES] if frames.is_dir() else []
    if not images:
        raise RuntimeError("course2md 没有生成截图")
    shutil.copytree(frames, destination / "frames")
    markdown = (raw / "course.md").read_text(encoding="utf-8")
    (destination / "source.md").write_text(markdown, encoding="utf-8")
    validate_images(destination / "source.md", prefix="frames")
    if (raw / "timeline.jsonl").is_file():
        timeline = destination / "timeline.jsonl"
        with (raw / "timeline.jsonl").open(encoding="utf-8") as source, timeline.open("w", encoding="utf-8") as target:
            for number, line in enumerate(source, 1):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"timeline.jsonl 第 {number} 行不是有效 JSON") from error
                if event.get("type") == "frame":
                    image = event.get("image")
                    if not isinstance(image, str) or not image.startswith("frames/"):
                        raise ValueError(f"timeline.jsonl 第 {number} 行截图路径无效：{image}")
                    relative = Path(image)
                    if ".." in relative.parts or not (destination / relative).is_file():
                        raise ValueError(f"timeline.jsonl 第 {number} 行截图不存在：{image}")
                target.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def localize_source_links(source, video, staged_video, video_relative):
    """Keep source links valid when the whole tutorial package is moved."""
    relative = "../input/" + quote(Path(video_relative).as_posix(), safe="/")
    path = source / "source.md"
    content = path.read_text(encoding="utf-8")
    for absolute_uri in {video.as_uri(), video.resolve().as_uri()}:
        content = content.replace(absolute_uri, relative)
    content = content.replace(str(staged_video), relative)
    path.write_text(content, encoding="utf-8")


def image_references(markdown_file, prefix="assets"):
    targets = IMAGE_LINK.findall(markdown_file.read_text(encoding="utf-8"))
    if not targets:
        raise RuntimeError(f"Markdown 没有图片引用：{markdown_file}")
    references = set()
    for target in targets:
        relative = Path(target.split("#", 1)[0].split("?", 1)[0])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != prefix:
            raise RuntimeError(f"图片必须引用当前目录的 {prefix}/：{target}")
        if relative.suffix.lower() not in IMAGE_SUFFIXES:
            raise RuntimeError(f"图片类型不支持：{target}")
        references.add(relative)
    return references


def validate_images(markdown_file, prefix="assets"):
    references = image_references(markdown_file, prefix)
    for relative in references:
        if not (markdown_file.parent / relative).is_file():
            raise RuntimeError(f"图片不存在：{relative}")
    return references


def validate_tutorial_structure(markdown_file):
    content = markdown_file.read_text(encoding="utf-8")
    if not re.search(r"(?m)^#\s+\S", content):
        raise RuntimeError("最终教程缺少一级标题")
    image_references(markdown_file)
    headings = "\n".join(re.findall(r"(?m)^#{2,}\s+(.+)$", content))
    introduction = re.match(r"(?s)^#\s+[^\n]+\n+(.*?)(?:\n#{2,}\s+|\Z)", content)
    has_intro_overview = bool(introduction and len(re.sub(r"\s+", "", introduction.group(1))) >= 40)
    required = {
        "前置条件": r"前置|准备|依赖|环境",
        "操作步骤": r"步骤|操作|流程|实操",
        "命令与配置": r"命令|配置|代码|参数",
        "常见问题": r"常见|问题|注意|踩坑|核对",
    }
    missing = [name for name, pattern in required.items() if not re.search(pattern, headings)]
    if not has_intro_overview and not re.search(r"概述|简介|目标", headings):
        missing.insert(0, "概述")
    if missing:
        raise RuntimeError(f"最终教程缺少必要章节：{', '.join(missing)}")
    if not re.search(r"(?m)^\s*(?:\d+\.|-)\s+.*(?:\d{1,2}:\d{2}|步骤|操作|命令|配置)", content):
        raise RuntimeError("最终教程缺少可执行步骤或时间点")


def copy_final_assets(output, extracted, cover=None):
    """Publish only images referenced by the final Markdown."""
    references = image_references(output / "tutorial.md")
    final_assets = output / "assets"
    if final_assets.exists():
        shutil.rmtree(final_assets)
    for relative in references:
        name = relative.relative_to("assets")
        source = (cover if cover and name == Path("cover" + cover.suffix.lower())
                  else extracted / "frames" / name)
        if not source.is_file():
            raise RuntimeError(f"最终教程引用的图片不存在：{relative}")
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    validate_images(output / "tutorial.md")


def version(command):
    flag = "-version" if command == "ffmpeg" else "--version"
    result = subprocess.run([command, flag], capture_output=True, text=True, check=True)
    output = result.stdout or result.stderr
    if not output:
        raise RuntimeError(f"{command} 未报告版本")
    return output.splitlines()[0].strip()


def build_source(stage, attempt, assets, title, video_relative, provider=None, asr_model=None):
    if shutil.which("course2md") is None or shutil.which("ffmpeg") is None:
        raise RuntimeError("缺少 course2md 或 ffmpeg")
    media_dir = stage / "media"
    media_dir.mkdir()
    video = assets["video"]
    staged_video = media_dir / "recording.mp4"
    if assets["audio"] and not assets["subtitle"]:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(assets["audio"]),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-y", str(staged_video),
        ], check=True)
    else:
        staged_video = media_dir / ("recording" + video.suffix.lower())
        staged_video.symlink_to(video)
    if assets["subtitle"]:
        sidecar = staged_video.with_suffix(assets["subtitle"].suffix.lower())
        shutil.copy2(assets["subtitle"], sidecar)
    source = "subtitle" if assets["subtitle"] else "asr"
    command = ["course2md", str(staged_video), "-o", str(stage / "notes"),
               "--transcript-source", source, "--no-llm", "--formats", "md", "--json"]
    if provider and source == "asr":
        command += ["--provider", provider]
    if asr_model and source == "asr":
        command += ["--asr-model", asr_model]
    environment = dict(os.environ)
    if source == "asr" and (provider == "coreml" or (provider is None and sys.platform == "darwin")):
        if not environment.get("QWEN3_CACHE_DIR"):
            cache = ROOT / ".models"
            cache.mkdir(parents=True, exist_ok=True)
            environment["QWEN3_CACHE_DIR"] = str(cache)
    log_path = attempt / "course2md.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, env=environment, bufsize=1, start_new_session=True)
        try:
            for line in process.stdout:
                log.write(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    print(line.rstrip(), flush=True)
                    continue
                if event.get("type") in {"stage", "error", "done"}:
                    print(line.rstrip(), flush=True)
            code = process.wait()
        except KeyboardInterrupt as error:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise RuntimeError("用户中断 course2md") from error
        finally:
            process.stdout.close()
    if code:
        errors = [line for line in log_path.read_text(encoding="utf-8").splitlines() if '"type":"error"' in line]
        detail = errors[-1] if errors else f"exit {code}"
        raise RuntimeError(f"course2md 失败：{detail}；日志：{log_path}")
    matches = list((stage / "notes").rglob("course.md"))
    if len(matches) != 1:
        raise RuntimeError(f"预期一份 course.md，实际找到 {len(matches)} 份")
    extracted = stage / "result"
    extracted.mkdir()
    normalize_source(matches[0].parent, extracted)
    localize_source_links(extracted, video, staged_video, video_relative)
    source_text = (extracted / "source.md").read_text(encoding="utf-8")
    source_text = re.sub(r"^# [^\n]+", lambda _: f"# {title}", source_text, count=1)
    (extracted / "source.md").write_text(source_text, encoding="utf-8")
    return extracted


def refine_settings(model_override=None, batch_size_override=None):
    path = ROOT / "workflow.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1:
        raise ValueError(f"不支持的工作流配置：{path}")
    model = model_override if model_override is not None else config.get("model")
    batch_size = batch_size_override if batch_size_override is not None else config.get("batch_size")
    if not isinstance(model, str) or not model.strip() or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("workflow.json 需要有效的 model 和正整数 batch_size")
    max_images = config.get("max_images_per_batch", MAX_IMAGES_PER_BATCH_DEFAULT)
    max_transcript_chars = config.get("max_transcript_chars_per_batch",
                                      MAX_TRANSCRIPT_CHARS_PER_BATCH_DEFAULT)
    overlap_seconds = config.get("context_overlap_seconds", CONTEXT_OVERLAP_SECONDS_DEFAULT)
    if not isinstance(max_images, int) or max_images < 1:
        raise ValueError("workflow.json 需要有效的 max_images_per_batch")
    if not isinstance(max_transcript_chars, int) or max_transcript_chars < 1:
        raise ValueError("workflow.json 需要有效的 max_transcript_chars_per_batch")
    if not isinstance(overlap_seconds, (int, float)) or overlap_seconds < 0:
        raise ValueError("workflow.json 需要有效的 context_overlap_seconds")
    if batch_size > max_images:
        raise ValueError(f"batch_size 不能超过 max_images_per_batch（{max_images}）")
    raw_prices = config.get("prices_usd_per_million_tokens", {}).get(model)
    if not isinstance(raw_prices, dict) or set(raw_prices) != {"input", "cached_input", "output"}:
        raise ValueError(f"workflow.json 缺少 {model} 的 input/cached_input/output 单价")
    try:
        prices = {name: Decimal(str(value)) for name, value in raw_prices.items()}
    except InvalidOperation as error:
        raise ValueError(f"{model} 的单价格式无效") from error
    if any(not value.is_finite() or value < 0 for value in prices.values()):
        raise ValueError(f"{model} 的单价必须为非负有限数字")
    return {"model": model, "batch_size": batch_size,
            "max_images_per_batch": max_images,
            "max_transcript_chars_per_batch": max_transcript_chars,
            "context_overlap_seconds": float(overlap_seconds),
            "prices": {name: str(value) for name, value in prices.items()},
            "pricing_source": config.get("pricing_source"),
            "pricing_checked_utc": config.get("pricing_checked_utc")}


def enrich_speech(event, cue_id):
    value = dict(event)
    value["cue_id"] = cue_id
    value.setdefault("end", value["start"])
    return value


def transcript_chars(events):
    return sum(len(event.get("text", "")) for event in events)


def load_timeline(extracted):
    path = extracted / "timeline.jsonl"
    if not path.is_file():
        raise RuntimeError("分批审阅需要 extracted/timeline.jsonl")
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    frames = sorted((event for event in events if event.get("type") == "frame"),
                    key=lambda event: (event["t"], event["image"]))
    speech = [enrich_speech(event, index + 1)
              for index, event in enumerate(sorted((event for event in events if event.get("type") == "speech"),
                                                   key=lambda event: event["start"]))]
    names = [event["image"] for event in frames]
    actual = {"frames/" + path.name for path in (extracted / "frames").iterdir()
              if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES}
    if not frames or len(names) != len(set(names)) or set(names) != actual:
        raise RuntimeError("时间线截图与 extracted/frames 不一致，无法保证全量审阅")
    if any(not isinstance(event.get("text"), str) or not isinstance(event.get("start"), (int, float))
           or not isinstance(event.get("end"), (int, float)) or event["start"] > event["end"]
           for event in speech):
        raise RuntimeError("时间线语音记录无效")
    return frames, speech


def speech_in_window(speech, start, end, overlap_seconds=0.0):
    left = max(0.0, start - overlap_seconds)
    right = end + overlap_seconds
    return [event for event in speech if event["start"] < right and event.get("end", event["start"]) >= left]


def plan_batches(extracted, settings, duration):
    frames, speech = load_timeline(extracted)
    batch_size = settings["batch_size"]
    max_chars = settings["max_transcript_chars_per_batch"]
    overlap = settings["context_overlap_seconds"]
    result = []
    cursor = 0
    while cursor < len(frames):
        selected = None
        for size in range(min(batch_size, len(frames) - cursor), 0, -1):
            group = frames[cursor:cursor + size]
            start = 0.0 if cursor == 0 else float(group[0]["t"])
            end = float(frames[cursor + size]["t"]) if cursor + size < len(frames) else float(duration)
            context_speech = speech_in_window(speech, start, end, overlap)
            if transcript_chars(context_speech) <= max_chars:
                primary = [event for event in speech if start <= event["start"] < end]
                selected = {"start": start, "end": end, "frames": group,
                            "speech": primary, "context_speech": context_speech}
                break
        if selected is None:
            raise RuntimeError(f"单张截图附近字幕超过 {max_chars} 字，无法形成安全批次")
        result.append({"batch_index": len(result) + 1,
                       "start_seconds": selected["start"],
                       "end_seconds": selected["end"],
                       "frames": selected["frames"],
                       "speech": selected["speech"],
                       "context_speech": selected["context_speech"],
                       "transcript_chars": transcript_chars(selected["context_speech"])})
        cursor += len(selected["frames"])
    if sum(len(group["speech"]) for group in result) != len(speech):
        raise RuntimeError("字幕时间超出分批范围，无法保证全量审阅")
    return result


def refine_plan(extracted, settings, duration, digest):
    groups = plan_batches(extracted, settings, duration)
    frames, speech = load_timeline(extracted)
    plan = {
        "schema_version": 1,
        "pipeline_version": REFINE_SCHEMA_VERSION,
        "prompt_version": REFINE_PROMPT_VERSION,
        "extraction_sha256": digest,
        "model": settings["model"],
        "html": False,
        "batch_size": settings["batch_size"],
        "max_images_per_batch": settings["max_images_per_batch"],
        "max_transcript_chars_per_batch": settings["max_transcript_chars_per_batch"],
        "context_overlap_seconds": settings["context_overlap_seconds"],
        "frame_count": len(frames),
        "speech_count": len(speech),
        "duration_seconds": duration,
        "batches": [{
            "batch_index": group["batch_index"],
            "start_seconds": group["start_seconds"],
            "end_seconds": group["end_seconds"],
            "frames": [event["image"] for event in group["frames"]],
            "speech_ids": [event["cue_id"] for event in group["speech"]],
            "context_speech_ids": [event["cue_id"] for event in group["context_speech"]],
            "transcript_chars": group["transcript_chars"],
        } for group in groups],
    }
    plan["plan_sha256"] = json_sha256(plan)
    return plan, groups


def codex_usage(trace, require_completed=True):
    total = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    completed = 0
    for line in trace.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            raise RuntimeError(f"Codex 用量事件缺少 usage：{trace}")
        for field in total:
            value = usage.get(field, 0 if field == "cached_input_tokens" else None)
            if not isinstance(value, int) or value < 0:
                raise RuntimeError(f"Codex 用量字段无效：{field}")
            total[field] += value
        completed += 1
    if require_completed and (not completed or total["cached_input_tokens"] > total["input_tokens"]):
        raise RuntimeError(f"Codex 未报告有效用量：{trace}")
    if not require_completed and total["cached_input_tokens"] > total["input_tokens"]:
        raise RuntimeError(f"Codex 用量缓存输入大于总输入：{trace}")
    return total


def record_codex_failure(attempt, tag, trace, errors, returncode):
    try:
        usage = codex_usage(trace, require_completed=False)
        has_usage = any(usage.values())
    except (OSError, json.JSONDecodeError, RuntimeError):
        usage, has_usage = None, False
    failure = {"tag": tag, "returncode": returncode, "trace": str(trace),
               "stderr": str(errors), "recorded_at_utc": now(),
               "billing_unknown": True}
    if CODEX_INIT_ERROR in errors.read_text(encoding="utf-8", errors="replace"):
        failure["failure_phase"] = "client_initialization"
    if has_usage:
        failure["completed_usage_reported_before_failure"] = usage
    with (attempt / "codex-failures.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(failure, ensure_ascii=False, separators=(",", ":")) + "\n")


def estimated_cost(usage, prices):
    input_tokens = usage["input_tokens"] - usage["cached_input_tokens"]
    value = (Decimal(input_tokens) * Decimal(prices["input"])
             + Decimal(usage["cached_input_tokens"]) * Decimal(prices["cached_input"])
             + Decimal(usage["output_tokens"]) * Decimal(prices["output"])) / Decimal(1000000)
    return str(value.quantize(Decimal("0.000001")))


def estimated_review_calls(plan):
    return len(plan["batches"]) + 1


def plan_preview(plan, settings):
    max_images = max(len(batch["frames"]) for batch in plan["batches"])
    max_chars = max(batch["transcript_chars"] for batch in plan["batches"])
    return {
        "plan_sha256": plan["plan_sha256"],
        "model": settings["model"],
        "frame_count": plan["frame_count"],
        "speech_count": plan["speech_count"],
        "batch_count": len(plan["batches"]),
        "planned_codex_calls": estimated_review_calls(plan),
        "max_images_in_batch": max_images,
        "max_transcript_chars_in_batch": max_chars,
        "prices_usd_per_million_tokens": settings["prices"],
        "billing_note": "dry_run_has_no_token_usage; final cost depends on Codex reported usage",
        "estimated_usd_note": "pre_call_token_count_unknown; usage.json reports completed calls after execution",
    }


def print_plan_preview(plan, settings):
    preview = plan_preview(plan, settings)
    print(json.dumps(preview, ensure_ascii=False, indent=2))


def confirm_paid_refine(plan, settings, yes, preview=None, new_codex_calls=None):
    if yes or new_codex_calls == 0:
        return
    print(json.dumps(preview or plan_preview(plan, settings), ensure_ascii=False, indent=2))
    if not sys.stdin.isatty():
        raise RuntimeError("即将调用 Codex 消耗 token；非交互运行请显式添加 --yes，或使用 --dry-run 只预览")
    answer = input("确认执行以上 Codex 调用？输入 yes 继续：").strip()
    if answer != "yes":
        raise RuntimeError("用户取消 generate")


def refine_options(settings, plan):
    return {"html": False, "model": settings["model"],
            "batch_size": settings["batch_size"],
            "max_images_per_batch": settings["max_images_per_batch"],
            "max_transcript_chars_per_batch": settings["max_transcript_chars_per_batch"],
            "context_overlap_seconds": settings["context_overlap_seconds"],
            "prompt_version": REFINE_PROMPT_VERSION,
            "plan_sha256": plan["plan_sha256"],
            "prices": settings["prices"]}


def refine_signature(settings, digest, plan):
    return {"plan_sha256": plan["plan_sha256"],
            "extraction_sha256": digest,
            "model": settings["model"],
            "prompt_version": REFINE_PROMPT_VERSION,
            "pipeline_version": REFINE_SCHEMA_VERSION}


def usage_report(calls, settings, run_id):
    def total_for(selected):
        total = {name: sum(call["usage"][name] for call in selected)
                 for name in ("input_tokens", "cached_input_tokens", "output_tokens")}
        cost = sum((Decimal(call["estimated_usd"]) for call in selected), Decimal("0"))
        return dict(total, estimated_usd=str(cost.quantize(Decimal("0.000001"))))

    return {"run_id": run_id, "model": settings["model"],
            "basis": "completed_calls_estimate_not_invoice",
            "prices_usd_per_million_tokens": settings["prices"],
            "pricing_source": settings["pricing_source"],
            "pricing_checked_utc": settings["pricing_checked_utc"],
            "calls": calls, "totals": total_for(calls),
            "attempt_totals": total_for([call for call in calls if "reused_from" not in call])}


def collect_failed_codex_calls(*attempts):
    failed = []
    for attempt in attempts:
        if not attempt:
            continue
        path = attempt / "codex-failures.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            failed.append({"run_id": attempt.name, "tag": record.get("tag"),
                           "returncode": record.get("returncode"),
                           "billing_unknown": True})
    return failed


def collect_uncommitted_completed_calls(settings, *attempts):
    calls = []
    for attempt in attempts:
        if not attempt:
            continue
        for result in sorted(attempt.glob("batch-*.result.json")):
            match = re.fullmatch(r"batch-(\d{4})\.result\.json", result.name)
            if not match:
                continue
            index = int(match.group(1))
            checkpoint = attempt / "batches" / f"batch-{index:04d}.json"
            if checkpoint.is_file():
                continue
            trace = attempt / f"batch-{index:04d}.jsonl"
            if not trace.is_file():
                continue
            try:
                usage = codex_usage(trace)
            except (OSError, json.JSONDecodeError, RuntimeError):
                continue
            calls.append({"run_id": attempt.name, "stage": "review", "batch_index": index,
                          "usage": usage,
                          "prices_usd_per_million_tokens": settings["prices"],
                          "estimated_usd": estimated_cost(usage, settings["prices"]),
                          "billing_status": "completed_not_checkpointed"})
        final_result = attempt / "final.result.md"
        final_trace = attempt / "final.jsonl"
        final_committed = False
        usage_report_path = attempt / "usage.json"
        if usage_report_path.is_file():
            try:
                existing = json.loads(usage_report_path.read_text(encoding="utf-8"))
                final_committed = any(call.get("stage") == "final"
                                      for call in existing.get("calls", [])
                                      if isinstance(call, dict))
            except (OSError, json.JSONDecodeError):
                final_committed = False
        if final_result.is_file() and final_trace.is_file() and not final_committed:
            try:
                usage = codex_usage(final_trace)
            except (OSError, json.JSONDecodeError, RuntimeError):
                continue
            calls.append({"run_id": attempt.name, "stage": "final",
                          "usage": usage,
                          "prices_usd_per_million_tokens": settings["prices"],
                          "estimated_usd": estimated_cost(usage, settings["prices"]),
                          "billing_status": "completed_not_checkpointed"})
    return calls


def uncommitted_usage_totals(calls):
    total = {name: sum(call["usage"][name] for call in calls)
             for name in ("input_tokens", "cached_input_tokens", "output_tokens")}
    cost = sum((Decimal(call["estimated_usd"]) for call in calls), Decimal("0"))
    return dict(total, estimated_usd=str(cost.quantize(Decimal("0.000001"))))


def attach_failed_billing_summary(report, failed_calls, uncommitted_calls=None):
    uncommitted_calls = uncommitted_calls or []
    report["uncommitted_completed_calls"] = uncommitted_calls
    report["uncommitted_completed_totals"] = uncommitted_usage_totals(uncommitted_calls)
    report["uncommitted_completed_note"] = \
        "completed Codex calls with usage that were not committed to batch checkpoints"
    report["unconfirmed_billing"] = {
        "billing_unknown": bool(failed_calls),
        "failed_calls": failed_calls,
        "note": "totals include completed calls only; failed Codex requests may have external billing not reported here",
    }
    return report


def write_failed_usage(attempt, settings, resume_from=None):
    path = attempt / "usage.json"
    report = (json.loads(path.read_text(encoding="utf-8")) if path.is_file()
              else usage_report([], settings, attempt.name))
    attach_failed_billing_summary(report, collect_failed_codex_calls(resume_from, attempt),
                                  collect_uncommitted_completed_calls(settings, resume_from, attempt))
    write_json(path, report)


def finish_failed_refine(attempt, error, settings, resume_from=None):
    for label, action in (("状态", lambda: finish_attempt(attempt, "failed", error)),
                          ("用量", lambda: write_failed_usage(attempt, settings, resume_from))):
        try:
            action()
        except Exception as diagnostic_error:
            print(f"警告：记录 generate 失败{label}时出错：{diagnostic_error}", file=sys.stderr)


def run_codex(workspace, prompt, images, model, attempt, tag, last_message, schema=None, writable=False):
    trace = attempt / f"{tag}.jsonl"
    errors = attempt / f"{tag}.stderr.log"
    command = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox",
               "workspace-write" if writable else "read-only", "-m", model,
               "-C", str(workspace), "-o", str(last_message)]
    if schema:
        command += ["--output-schema", str(schema)]
    command.append(prompt)
    for path in images:
        command += ["--image", str(path)]
    protected = tree_hashes(workspace)
    with trace.open("w", encoding="utf-8") as stdout, errors.open("w", encoding="utf-8") as stderr:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        except KeyboardInterrupt:
            record_codex_failure(attempt, tag, trace, errors, None)
            raise
    if result.returncode:
        record_codex_failure(attempt, tag, trace, errors, result.returncode)
        failure_hint = ("；Codex CLI 初始化被系统拒绝（Operation not permitted）"
                        if CODEX_INIT_ERROR in errors.read_text(encoding="utf-8", errors="replace") else "")
        raise RuntimeError(f"Codex {tag} 失败（exit {result.returncode}）{failure_hint}；日志：{trace}、{errors}")
    for relative, digest in protected.items():
        path = workspace / relative
        if not path.is_file() or hash_file(path) != digest:
            raise RuntimeError(f"Codex 修改了受保护的文件：{relative}")
    added = set(tree_hashes(workspace)) - set(protected)
    expected = {Path("output/tutorial.draft.md")} if writable else set()
    if added != expected:
        raise RuntimeError(f"Codex 生成了意外文件：{sorted(str(path) for path in added)}")
    if not last_message.is_file():
        raise RuntimeError(f"Codex 未写入结果：{last_message}")
    return codex_usage(trace)


def validate_batch_note(note, group):
    if not isinstance(note, dict) or not isinstance(note.get("frames"), list):
        raise RuntimeError("批次结果缺少逐图观察")
    expected = [event["image"] for event in group["frames"]]
    actual = [item.get("image") for item in note["frames"] if isinstance(item, dict)]
    if actual != expected:
        raise RuntimeError(f"批次 {group['batch_index']} 未逐张覆盖截图：{expected} / {actual}")
    if any(not isinstance(item.get("observation"), str) or not item["observation"].strip()
           or not isinstance(item.get("tutorial_value"), str) or not item["tutorial_value"].strip()
           for item in note["frames"]):
        raise RuntimeError("批次逐图观察不能为空")
    if not isinstance(note.get("notes_md"), str) or not note["notes_md"].strip():
        raise RuntimeError("批次结果缺少笔记")
    if not isinstance(note.get("carry_forward"), str) or len(note["carry_forward"]) > 1600:
        raise RuntimeError("批次交接摘要无效或超过 1600 字")


def normalize_batch_note_images(note, group, workspace_extracted):
    if not isinstance(note, dict):
        raise RuntimeError("批次结果不是 JSON 对象")
    normalized = json.loads(json.dumps(note, ensure_ascii=False))
    frames = normalized.get("frames")
    if not isinstance(frames, list):
        return normalized
    expected = {event["image"] for event in group["frames"]}
    base = workspace_extracted.resolve(strict=False)
    for item in frames:
        if not isinstance(item, dict) or not isinstance(item.get("image"), str):
            continue
        image = item["image"]
        if image in expected:
            continue
        image_path = Path(image)
        if not image_path.is_absolute():
            continue
        try:
            relative = image_path.resolve(strict=False).relative_to(base)
        except ValueError as error:
            raise RuntimeError(f"批次图片绝对路径不在本次 workspace/extracted 下：{image}") from error
        if relative.parts[:1] != ("frames",) or relative.as_posix() not in expected:
            raise RuntimeError(f"批次图片绝对路径不能匹配计划截图：{image}")
        item["image"] = relative.as_posix()
    return normalized


def carry_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def checkpoint_context_hash(group):
    value = {"batch_index": group["batch_index"],
             "start_seconds": group["start_seconds"],
             "end_seconds": group["end_seconds"],
             "frames": [event["image"] for event in group["frames"]],
             "speech_ids": [event["cue_id"] for event in group["speech"]],
             "context_speech_ids": [event["cue_id"] for event in group["context_speech"]]}
    return json_sha256(value)


def validate_batch_checkpoint(saved, group, plan, previous_context):
    if saved.get("schema_version") != 1:
        raise RuntimeError("批次检查点 schema_version 无效")
    if saved.get("plan_sha256") != plan["plan_sha256"]:
        raise RuntimeError("批次检查点与当前计划不匹配")
    if saved.get("prompt_version") != REFINE_PROMPT_VERSION:
        raise RuntimeError("批次检查点 prompt_version 不匹配")
    if saved.get("batch_index") != group["batch_index"]:
        raise RuntimeError("批次检查点序号不匹配")
    expected_frames = [event["image"] for event in group["frames"]]
    if saved.get("frames") != expected_frames:
        raise RuntimeError("批次检查点截图列表不匹配")
    if saved.get("previous_context_sha256") != carry_hash(previous_context):
        raise RuntimeError("批次检查点上一批上下文不匹配")
    if saved.get("context_sha256") != checkpoint_context_hash(group):
        raise RuntimeError("批次检查点上下文不匹配")
    if not isinstance(saved.get("usage"), dict) or saved.get("billing_status") != "completed":
        raise RuntimeError("批次检查点缺少完成用量")
    validate_batch_note(saved.get("note"), group)


def build_batch_checkpoint(index, group, plan, previous_context, note, usage, settings, recovered_from=None):
    checkpoint = {
        "schema_version": 1,
        "batch_index": index,
        "plan_sha256": plan["plan_sha256"],
        "prompt_version": REFINE_PROMPT_VERSION,
        "context_sha256": checkpoint_context_hash(group),
        "previous_context_sha256": carry_hash(previous_context),
        "frames": [event["image"] for event in group["frames"]],
        "note": note,
        "usage": usage,
        "billing_status": "completed",
        "prices_usd_per_million_tokens": settings["prices"],
        "estimated_usd": estimated_cost(usage, settings["prices"]),
        "completed_at_utc": now(),
    }
    if recovered_from:
        checkpoint["recovered_from_raw_result"] = recovered_from
    return checkpoint


def load_raw_completed_batch(candidate, group, plan, previous_context, settings):
    index = group["batch_index"]
    result = candidate / f"batch-{index:04d}.result.json"
    trace = candidate / f"batch-{index:04d}.jsonl"
    if not result.is_file() or not trace.is_file():
        return None
    note = json.loads(result.read_text(encoding="utf-8"))
    note = normalize_batch_note_images(note, group, candidate / ".staging" / "workspace" / "extracted")
    validate_batch_note(note, group)
    usage = codex_usage(trace)
    checkpoint = build_batch_checkpoint(index, group, plan, previous_context, note, usage,
                                        settings, recovered_from=candidate.name)
    validate_batch_checkpoint(checkpoint, group, plan, previous_context)
    return checkpoint


def load_raw_completed_final(candidate, workspace, settings):
    result = candidate / "final.result.md"
    trace = candidate / "final.jsonl"
    if not result.is_file() or not trace.is_file():
        return None
    markdown = result.read_text(encoding="utf-8").strip()
    if not markdown.startswith("#") or "![" not in markdown:
        raise RuntimeError(f"已完成 final 结果缺少标题或图片引用：{result}")
    candidate_md = workspace / "output" / "tutorial.md"
    candidate_md.write_text(markdown + "\n", encoding="utf-8")
    validate_tutorial_structure(candidate_md)
    usage = codex_usage(trace)
    return {"usage": usage, "estimated_usd": estimated_cost(usage, settings["prices"]),
            "reused_from": candidate.name}


def raw_final_candidate_reusable(candidate, settings):
    if not candidate:
        return False
    result = candidate / "final.result.md"
    trace = candidate / "final.jsonl"
    if not result.is_file() or not trace.is_file():
        return False
    with tempfile.TemporaryDirectory(prefix="tutorial-final-preview-") as temporary:
        output = Path(temporary) / "output"
        output.mkdir()
        markdown = result.read_text(encoding="utf-8").strip()
        if not markdown.startswith("#") or "![" not in markdown:
            return False
        (output / "tutorial.md").write_text(markdown + "\n", encoding="utf-8")
        validate_tutorial_structure(output / "tutorial.md")
    codex_usage(trace)
    return True


def attempt_started_at(attempt):
    status = attempt / "status.json"
    if not status.is_file():
        return None
    try:
        value = json.loads(status.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    started = value.get("started_at_utc")
    return started if isinstance(started, str) else None


def previous_batch_attempt(logs, current, signature, fresh=False, after_run_id=None):
    if fresh:
        return None
    lower_bound_started = attempt_started_at(logs / after_run_id) if after_run_id else None
    if after_run_id and lower_bound_started is None:
        return None
    candidates = []
    for candidate in logs.iterdir():
        if candidate == current or not candidate.is_dir() or candidate.is_symlink():
            continue
        started = attempt_started_at(candidate)
        if lower_bound_started is not None and (started is None or started <= lower_bound_started):
            continue
        candidates.append((started or "", candidate))
    for _, candidate in sorted(candidates, reverse=True):
        state = candidate / "refine-state.json"
        status = candidate / "status.json"
        recoverable = (list((candidate / "batches").glob("batch-*.json"))
                       or list(candidate.glob("batch-*.result.json"))
                       or (candidate / "final.result.md").is_file())
        if state.is_file() and status.is_file() and recoverable and json.loads(state.read_text()) == signature:
            if json.loads(status.read_text()).get("status") in {"failed", "running"}:
                return candidate
    return None


def reusable_batch_prefix(resume_from, groups, plan, settings):
    if not resume_from:
        return []
    reused = []
    previous_context = ""
    for group in groups:
        index = group["batch_index"]
        cached = resume_from / "batches" / f"batch-{index:04d}.json"
        checkpoint = None
        if cached.is_file():
            checkpoint = json.loads(cached.read_text(encoding="utf-8"))
            validate_batch_checkpoint(checkpoint, group, plan, previous_context)
        else:
            checkpoint = load_raw_completed_batch(resume_from, group, plan, previous_context, settings)
        if not checkpoint:
            break
        reused.append(index)
        previous_context = checkpoint["note"]["carry_forward"]
    return reused


def generate_tutorial(output, extracted, attempt, title, settings, plan, groups, cover=None, resume_from=None):
    if shutil.which("codex") is None:
        raise RuntimeError("缺少 codex CLI")
    workspace = output.parent
    shutil.copytree(extracted, workspace / "extracted")
    analysis = workspace / "analysis"
    analysis.mkdir()
    checkpoints = attempt / "batches"
    checkpoints.mkdir()
    schema = workspace / "batch.schema.json"
    write_json(schema, BATCH_SCHEMA)
    calls = []
    reused_batches = []
    resume_reused_batches = []
    previous_context = ""
    resume_prefix_open = bool(resume_from)
    for group in groups:
        index = group["batch_index"]
        name = f"batch-{index:04d}.json"
        checkpoint = checkpoints / name
        cached = resume_from / "batches" / name if resume_from else None
        call_prices = settings["prices"]
        handled = False
        if resume_prefix_open and cached and cached.is_file():
            saved = json.loads(cached.read_text(encoding="utf-8"))
            try:
                validate_batch_checkpoint(saved, group, plan, previous_context)
            except RuntimeError as error:
                raise RuntimeError(f"批次检查点无效：{cached}；{error}") from error
            shutil.copy2(cached, checkpoint)
            note, usage = saved["note"], saved["usage"]
            call_prices = saved.get("prices_usd_per_million_tokens", settings["prices"])
            call_estimated = saved.get("estimated_usd") or estimated_cost(usage, call_prices)
            reused_batches.append(index)
            resume_reused_batches.append(index)
            handled = True
        elif resume_prefix_open and resume_from:
            recovered = load_raw_completed_batch(resume_from, group, plan, previous_context, settings)
            if recovered:
                write_json(checkpoint, recovered)
                note, usage = recovered["note"], recovered["usage"]
                call_prices = recovered["prices_usd_per_million_tokens"]
                call_estimated = recovered["estimated_usd"]
                reused_batches.append(index)
                resume_reused_batches.append(index)
                handled = True
        if not handled:
            resume_prefix_open = False
            context = workspace / "batch_context.json"
            write_json(context, {"title": title, "batch_index": index,
                                 "plan_sha256": plan["plan_sha256"],
                                 "prompt_version": REFINE_PROMPT_VERSION,
                                 "start_seconds": group["start_seconds"],
                                 "end_seconds": group["end_seconds"],
                                 "frames": group["frames"], "speech": group["speech"],
                                 "context_speech": group["context_speech"],
                                 "previous_confirmed_context": previous_context})
            last_message = attempt / f"batch-{index:04d}.result.json"
            prompt = ("审阅本批附带的每一张视频截图，并读取 batch_context.json 中本时间段的完整字幕。"
                      "speech 是本批主字幕，context_speech 是相邻边界上下文；引用字幕时优先使用 cue_id。"
                      "按截图顺序输出 JSON：frames 每项写准确的 image 路径、画面观察和教程价值；"
                      "frames[].image 必须逐项照抄 batch_context.json 中对应 frames[].image 的相对路径，"
                      "例如 frames/slide_0001.jpg，禁止输出绝对路径；"
                      "notes_md 记录可操作步骤、命令、时间戳及不确定点；"
                      "carry_forward 只写下一批需要的已确认术语和上下文，不超过 800 字。"
                      "上一批摘要仅用于连续性，遇到本批素材冲突以本批为准。"
                      "不要把字幕没证实的画面细节写成事实。只输出 JSON，不修改任何文件。")
            images = [workspace / "extracted" / event["image"] for event in group["frames"]]
            usage = run_codex(workspace, prompt, images, settings["model"], attempt,
                              f"batch-{index:04d}", last_message, schema=schema)
            note = json.loads(last_message.read_text(encoding="utf-8"))
            note = normalize_batch_note_images(note, group, workspace / "extracted")
            validate_batch_note(note, group)
            call_estimated = estimated_cost(usage, settings["prices"])
            write_json(checkpoint, build_batch_checkpoint(index, group, plan, previous_context,
                                                          note, usage, settings))
        write_json(analysis / name, {"batch_index": index,
                                      "start_seconds": group["start_seconds"],
                                      "end_seconds": group["end_seconds"], "note": note})
        call = {"stage": "review", "batch_index": index, "usage": usage,
                "prices_usd_per_million_tokens": call_prices,
                "estimated_usd": call_estimated}
        if index in reused_batches:
            call["reused_from"] = resume_from.name
        calls.append(call)
        write_json(attempt / "usage.json",
                   attach_failed_billing_summary(usage_report(calls, settings, attempt.name),
                                                 collect_failed_codex_calls(resume_from, attempt),
                                                 collect_uncommitted_completed_calls(settings, attempt)))
        previous_context = note["carry_forward"]
        print(f"审阅截图：{index}/{len(groups)}（本批 {len(group['frames'])} 张）", flush=True)
    context = workspace / "batch_context.json"
    if context.exists():
        context.unlink()
    schema.unlink()
    if cover:
        final_assets = output / "assets"
        final_assets.mkdir(exist_ok=True)
        shutil.copy2(cover, final_assets / ("cover" + cover.suffix.lower()))
    can_reuse_final = bool(resume_from and len(resume_reused_batches) == len(groups))
    recovered_final = load_raw_completed_final(resume_from, workspace, settings) if can_reuse_final else None
    if recovered_final:
        shutil.copy2(resume_from / "final.result.md", attempt / "final.result.md")
        shutil.copy2(resume_from / "final.jsonl", attempt / "final.jsonl")
        calls.append({"stage": "final", "usage": recovered_final["usage"],
                      "prices_usd_per_million_tokens": settings["prices"],
                      "estimated_usd": recovered_final["estimated_usd"],
                      "reused_from": recovered_final["reused_from"]})
    else:
        prompt = (f"阅读 extracted/source.md、extracted/timeline.jsonl 和 analysis/batch-*.json，"
                  f"为《{title}》输出一份中文图文实操教程 Markdown 正文。"
                  "逐批整合全部笔记，覆盖视频全程的重要操作，不把字幕逐句照抄。"
                  "包含概述、前置条件、按时间顺序的步骤、命令/配置、预期结果和常见问题。"
                  "每个可执行步骤标视频时间点；逐图观察与字幕有冲突时写‘待核对’，不要编造。"
                  "图片可引用 extracted/frames 中任何已审阅截图，但最终 Markdown 路径必须写成 assets/同名文件。"
                  "若 output/assets/ 下有 cover 图片，可按内容需要引用。"
                  "不要修改任何文件；只返回 Markdown，不要包裹代码围栏。")
        last_message = attempt / "final.result.md"
        usage = run_codex(workspace, prompt, [], settings["model"], attempt,
                          "final", last_message, writable=False)
        calls.append({"stage": "final", "usage": usage,
                      "prices_usd_per_million_tokens": settings["prices"],
                      "estimated_usd": estimated_cost(usage, settings["prices"])})
        markdown = last_message.read_text(encoding="utf-8").strip()
        if not markdown.startswith("#") or "![" not in markdown:
            raise RuntimeError("Codex 最终 Markdown 缺少标题或图片引用")
        (output / "tutorial.md").write_text(markdown + "\n", encoding="utf-8")
        validate_tutorial_structure(output / "tutorial.md")
    report = attach_failed_billing_summary(usage_report(calls, settings, attempt.name),
                                           collect_failed_codex_calls(resume_from, attempt),
                                           collect_uncommitted_completed_calls(settings, attempt))
    write_json(attempt / "usage.json", report)
    return report, reused_batches


def render_html(output, title):
    if shutil.which("pandoc") is None:
        raise RuntimeError("需要 pandoc 才能生成最终 tutorial.html")
    source = output / "tutorial.md"
    source_hash = hash_file(source)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tutorial-", suffix=".html", dir=output)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        subprocess.run(["pandoc", "tutorial.md", "--standalone", "--metadata", f"pagetitle={title}",
                        "-o", temporary.name], cwd=output, check=True)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("pandoc 未生成有效的 tutorial.html")
        if hash_file(source) != source_hash:
            raise RuntimeError("导出期间 tutorial.md 已变化；HTML 未发布")
        os.chmod(temporary, source.stat().st_mode & 0o777)
        os.replace(temporary, output / "tutorial.html")
    finally:
        temporary.unlink(missing_ok=True)


def export_html(package, dry_run=False):
    def inputs():
        record = read_output(package)
        if not record or record.get("status") != "completed":
            raise ValueError("没有已完成的 output；请先运行 generate")
        source = package / "output" / "tutorial.md"
        if not source.is_file():
            raise ValueError(f"缺少最终教程：{source}")
        validate_images(source)
        title = record.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("output/run.json 缺少有效标题")
        return source, title

    if dry_run:
        source, _ = inputs()
        target = source.with_suffix(".html")
        print(json.dumps({"operation": "html", "source": str(source), "target": str(target),
                          "action": "replace" if target.exists() else "create",
                          "pandoc_available": shutil.which("pandoc") is not None,
                          "codex_calls": 0, "dry_run_has_side_effects": False},
                         ensure_ascii=False, indent=2))
        return target
    with package_lock(package):
        source, title = inputs()
        render_html(source.parent, title)
        target = source.with_suffix(".html")
        print(f"HTML：{target}")
        return target


def extraction_record(contract, fingerprints, source_options, video_duration, digest, run_id):
    return {
        "schema_version": 1, "title": contract["title"], "status": "ready",
        "video_duration_seconds": video_duration,
        "created_at_utc": now(), "run_id": run_id,
        "assets": fingerprints, "source_options": source_options,
        "extraction_sha256": digest,
        "tools": {"course2md": version("course2md"), "ffmpeg": version("ffmpeg")},
    }


def output_record(contract, digest, options, usage, run_id, resumed_from=None, reused_batches=None):
    return {
        "schema_version": REFINE_SCHEMA_VERSION, "title": contract["title"], "status": "completed",
        "created_at_utc": now(), "run_id": run_id,
        "resumed_from": resumed_from if reused_batches else None,
        "reused_batches": reused_batches or [], "extraction_sha256": digest,
        "refine_options": options, "usage": usage["totals"],
        "tools": {"codex": version("codex"), "pandoc": None},
    }


def refine_candidate(candidate, extracted, attempt, title, settings, plan, groups,
                     cover=None, resume_from=None):
    usage, reused_batches = generate_tutorial(candidate, extracted, attempt, title, settings,
                                              plan, groups, cover, resume_from)
    copy_final_assets(candidate, extracted, cover)
    shutil.copytree(candidate.parent / "analysis", candidate / "analysis")
    write_json(candidate / "usage.json", usage)
    return usage, reused_batches


def text_source_label(assets):
    if assets["subtitle"]:
        return "subtitle"
    if assets["audio"]:
        return "audio_asr"
    return "video_audio_asr"


def extract_preview(package, provider=None, asr_model=None, overwrite=False):
    if asr_model and provider not in (None, "coreml"):
        raise ValueError("--asr-model 只能与 CoreML 后端一起使用")
    contract, assets = check(package, verbose=False)
    if assets["subtitle"] and (provider or asr_model):
        raise ValueError("已提供字幕，不需要 --provider 或 --asr-model")
    source_options = {"provider": provider, "asr_model": asr_model}
    hashes = asset_hashes(assets)
    fingerprints = asset_fingerprints(contract, hashes)
    existing = read_extraction(package)
    action = "create"
    reason = "no_existing_extracted"
    will_run_course2md = True
    if existing:
        same_contract = (existing.get("schema_version") == 1
                         and existing.get("assets") == fingerprints
                         and existing.get("source_options") == source_options)
        same_digest = False
        extracted = package / "extracted"
        if same_contract and (extracted / "source.md").is_file():
            try:
                same_digest = existing.get("extraction_sha256") == extraction_digest(extracted)
            except OSError:
                same_digest = False
        if same_contract and same_digest and not overwrite:
            action = "reuse"
            reason = "existing_extracted_matches_inputs"
            will_run_course2md = False
        elif overwrite:
            action = "replace"
            reason = "overwrite_requested"
            will_run_course2md = True
        else:
            action = "blocked_requires_overwrite"
            reason = "existing_extracted_differs"
            will_run_course2md = False
    summaries = {
        "create": "将生成 extracted 原始图文稿",
        "reuse": "将复用已有 extracted 原始图文稿",
        "replace": "将替换 extracted 原始图文稿",
        "blocked_requires_overwrite": "已有 extracted 与当前输入不一致；需要 --overwrite",
    }
    preview = {
        "operation": "extract",
        "package": str(package),
        "target": str(package / "extracted"),
        "action": action,
        "reason": reason,
        "summary": summaries[action],
        "title": contract["title"],
        "text_source": text_source_label(assets),
        "source_options": source_options,
        "will_run_course2md": will_run_course2md,
        "dry_run_has_side_effects": False,
    }
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    return preview


def execute_extract(package, provider=None, asr_model=None, overwrite=False, dry_run=False):
    if dry_run:
        extract_preview(package, provider, asr_model, overwrite)
        return package / "extracted"
    if asr_model and provider not in (None, "coreml"):
        raise ValueError("--asr-model 只能与 CoreML 后端一起使用")
    contract, assets = check(package, verbose=False)
    if assets["subtitle"] and (provider or asr_model):
        raise ValueError("已提供字幕，不需要 --provider 或 --asr-model")
    source_options = {"provider": provider, "asr_model": asr_model}
    with package_lock(package) as logs:
        mark_stale_running_attempts(logs)
        recover_directories(package, logs)
        hashes = asset_hashes(assets)
        fingerprints = asset_fingerprints(contract, hashes)
        existing = read_extraction(package)
        if existing and not overwrite:
            if existing.get("schema_version") != 1 or existing.get("assets") != fingerprints or existing.get("source_options") != source_options:
                raise ValueError("输入或提取参数与现有结果不同；请使用 extract --overwrite")
            extracted = package / "extracted"
            validate_images(extracted / "source.md", prefix="frames")
            if existing.get("extraction_sha256") != extraction_digest(extracted):
                raise ValueError("提取结果已变化；请使用 extract --overwrite")
            print(f"复用已有提取：{extracted}")
            return extracted
        attempt, stage = begin_attempt(logs, "extract")
        try:
            extracted = build_source(stage, attempt, assets, contract["title"],
                                     contract["assets"]["video"], provider, asr_model)
            assert_input_stable(package, contract, assets, hashes)
            record = extraction_record(contract, fingerprints, source_options,
                                       probe(assets["video"])[0], extraction_digest(extracted),
                                       attempt.name)
            write_json(extracted / "extraction.json", record)
            result = publish_directory(package, "extracted", extracted, attempt)
            finish_attempt(attempt, "completed")
            return result
        except Exception as error:
            finish_attempt(attempt, "failed", error)
            raise RuntimeError(f"提取失败；日志：{attempt}；原因：{error}") from error


def prepare_refine_inputs(package, settings):
    contract, assets = check(package, verbose=False)
    extraction = read_extraction(package)
    extracted = package / "extracted"
    if not extraction or extraction.get("schema_version") != 1 or not (extracted / "source.md").is_file():
        raise ValueError("没有可用的原始提取；请先运行 extract")
    hashes = asset_hashes(assets)
    if extraction.get("assets") != asset_fingerprints(contract, hashes):
        raise ValueError("输入素材已变化；请先运行 extract --overwrite")
    digest = extraction_digest(extracted)
    if extraction.get("extraction_sha256") != digest:
        raise ValueError("提取结果已变化；请先运行 extract --overwrite")
    validate_images(extracted / "source.md", prefix="frames")
    plan, groups = refine_plan(extracted, settings, extraction["video_duration_seconds"], digest)
    return contract, assets, extraction, extracted, hashes, digest, plan, groups


def generate_preview(package, settings, overwrite=False, fresh=False):
    contract, assets, extraction, extracted, hashes, digest, plan, groups = \
        prepare_refine_inputs(package, settings)
    existing = read_output(package)
    options = refine_options(settings, plan)
    preview = plan_preview(plan, settings)
    preview.update({
        "operation": "generate",
        "package": str(package),
        "target": str(package / "output"),
        "title": contract["title"],
        "output_action": "create",
        "output_reuse": False,
        "overwrite": overwrite,
        "fresh": fresh,
        "resume_candidate": None,
        "reused_contiguous_batches": [],
        "pending_batches": [group["batch_index"] for group in groups],
        "final_call_needed": True,
        "new_codex_calls": len(groups) + 1,
        "dry_run_has_side_effects": False,
    })
    if existing and not overwrite:
        if existing.get("schema_version") != REFINE_SCHEMA_VERSION or existing.get("status") != "completed":
            preview.update({"output_action": "blocked_requires_overwrite",
                            "reason": "existing_output_schema_or_status_unsupported",
                            "pending_batches": [], "final_call_needed": False,
                            "new_codex_calls": 0})
            return preview
        if existing.get("extraction_sha256") == digest and existing.get("refine_options") == options:
            validate_images(package / "output" / "tutorial.md")
            validate_tutorial_structure(package / "output" / "tutorial.md")
            preview.update({"output_action": "reuse_output", "output_reuse": True,
                            "reason": "existing_output_matches_inputs",
                            "pending_batches": [], "final_call_needed": False,
                            "new_codex_calls": 0})
            return preview
        preview.update({"output_action": "blocked_requires_overwrite",
                        "reason": "existing_output_differs",
                        "pending_batches": [], "final_call_needed": False,
                        "new_codex_calls": 0})
        return preview
    if existing and overwrite:
        preview["output_action"] = "replace"
    logs = package / "log"
    resume_from = None
    if logs.is_dir():
        signature = refine_signature(settings, digest, plan)
        recovery_after_run_id = existing.get("run_id") if overwrite and existing else None
        resume_from = previous_batch_attempt(logs, logs / ".dry-run", signature, fresh=fresh,
                                             after_run_id=recovery_after_run_id)
    reused = reusable_batch_prefix(resume_from, groups, plan, settings)
    pending = [group["batch_index"] for group in groups if group["batch_index"] not in set(reused)]
    final_reusable = bool(resume_from and len(reused) == len(groups)
                          and raw_final_candidate_reusable(resume_from, settings))
    preview.update({
        "resume_candidate": resume_from.name if resume_from else None,
        "reused_contiguous_batches": reused,
        "pending_batches": pending,
        "final_call_needed": not final_reusable,
        "new_codex_calls": len(pending) + (0 if final_reusable else 1),
        "reason": "will_generate_or_resume",
    })
    return preview


def execute_refine(package, model=None, batch_size=None, overwrite=False,
                   yes=True, dry_run=False, fresh=False):
    settings = refine_settings(model, batch_size)
    if dry_run:
        print(json.dumps(generate_preview(package, settings, overwrite, fresh),
                         ensure_ascii=False, indent=2))
        return package / "output"
    with package_lock(package) as logs:
        mark_stale_running_attempts(logs)
        recover_directories(package, logs)
        contract, assets, extraction, extracted, hashes, digest, plan, groups = \
            prepare_refine_inputs(package, settings)
        existing = read_output(package)
        options = refine_options(settings, plan)
        if existing and not overwrite:
            if existing.get("schema_version") != REFINE_SCHEMA_VERSION or existing.get("status") != "completed":
                raise ValueError("现有最终结果格式不支持；请使用 generate --overwrite")
            if existing.get("extraction_sha256") == digest and existing.get("refine_options") == options:
                validate_images(package / "output" / "tutorial.md")
                validate_tutorial_structure(package / "output" / "tutorial.md")
                print(f"复用已有输出：{package / 'output'}")
                return package / "output"
            raise ValueError("教程已完成；如需替换，请使用 generate --overwrite")
        signature = refine_signature(settings, digest, plan)
        recovery_after_run_id = existing.get("run_id") if overwrite and existing else None
        resume_from = previous_batch_attempt(logs, logs / ".pending-refine", signature, fresh=fresh,
                                             after_run_id=recovery_after_run_id)
        reused_preview = reusable_batch_prefix(resume_from, groups, plan, settings)
        final_reusable = bool(resume_from and len(reused_preview) == len(groups)
                              and raw_final_candidate_reusable(resume_from, settings))
        pending_batches = [group["batch_index"] for group in groups
                           if group["batch_index"] not in set(reused_preview)]
        new_codex_calls = len(pending_batches) + (0 if final_reusable else 1)
        paid_preview = plan_preview(plan, settings)
        paid_preview.update({"operation": "generate",
                             "resume_candidate": resume_from.name if resume_from else None,
                             "reused_contiguous_batches": reused_preview,
                             "pending_batches": pending_batches,
                             "final_call_needed": not final_reusable,
                             "new_codex_calls": new_codex_calls})
        confirm_paid_refine(plan, settings, yes, paid_preview, new_codex_calls)
        attempt, stage = begin_attempt(logs, "refine")
        write_json(attempt / "refine-state.json", signature)
        write_json(attempt / "plan.json", plan)
        if resume_from:
            attempt_status = json.loads((attempt / "status.json").read_text(encoding="utf-8"))
            attempt_status["resume_candidate"] = resume_from.name
            write_json(attempt / "status.json", attempt_status)
        try:
            candidate = stage / "workspace" / "output"
            candidate.mkdir(parents=True)
            usage, reused_batches = refine_candidate(candidate, extracted, attempt, contract["title"],
                                                     settings, plan, groups,
                                                     assets["cover"], resume_from)
            assert_input_stable(package, contract, assets, hashes)
            if read_extraction(package) != extraction or extraction_digest(extracted) != digest:
                raise RuntimeError("处理期间原始提取发生变化；结果未发布")
            record = output_record(contract, digest, options, usage, attempt.name,
                                   resume_from.name if resume_from else None, reused_batches)
            write_json(candidate / "run.json", record)
            if reused_batches:
                attempt_status = json.loads((attempt / "status.json").read_text(encoding="utf-8"))
                attempt_status["resumed_from"] = resume_from.name
                attempt_status["reused_batches"] = reused_batches
                write_json(attempt / "status.json", attempt_status)
            result = publish_directory(package, "output", candidate, attempt)
            finish_attempt(attempt, "completed")
            return result
        except Exception as error:
            finish_failed_refine(attempt, error, settings, resume_from)
            raise RuntimeError(f"generate 失败；原有输出未改变；日志：{attempt}；原因：{error}") from error
        except KeyboardInterrupt as error:
            finish_failed_refine(attempt, "用户中断 generate", settings, resume_from)
            raise RuntimeError(f"用户中断 generate；原有输出未改变；日志：{attempt}") from error


def status(package):
    extraction = read_extraction(package)
    existing = read_output(package)
    extracted = package / "extracted"
    output = package / "output"
    print(f"提取：{extraction['status'] if extraction else 'empty'}  {extracted}")
    if (extracted / "source.md").is_file():
        print(f"原始图文稿：{extracted / 'source.md'}")
    output_status = existing["status"] if existing else "empty"
    if existing and (not extraction or existing.get("extraction_sha256") != extraction_digest(extracted)):
        output_status = "stale"
    print(f"输出：{output_status}  {output}")
    if existing and existing.get("run_id"):
        print(f"处理 ID：{existing['run_id']}  {package / 'log' / existing['run_id']}")
    for label, relative in (("最终教程", "tutorial.md"), ("HTML", "tutorial.html")):
        path = output / relative
        if path.is_file():
            print(f"{label}：{path}")
    if existing and isinstance(existing.get("usage"), dict):
        usage = existing["usage"]
        print(f"用量：输入 {usage['input_tokens']}、缓存输入 {usage['cached_input_tokens']}、"
              f"输出 {usage['output_tokens']} tokens；估算 ${usage['estimated_usd']}")
    logs = package / "log"
    if logs.is_dir():
        for attempt in sorted(logs.iterdir()):
            record = attempt / "status.json"
            if record.is_file():
                state = json.loads(record.read_text(encoding="utf-8"))
                print(f"{state['status']}：{display_action(state['action'])}  {attempt}")


def display_action(action):
    return "generate" if action == "refine" else action


def directory_size(path):
    total = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            continue
        if item.is_file():
            total += item.stat().st_size
    return total


def attempt_status(attempt):
    if not attempt.is_dir() or attempt.is_symlink():
        return None
    status = attempt / "status.json"
    if not status.is_file():
        return None
    try:
        value = json.loads(status.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if value.get("run_id") != attempt.name:
        return None
    if value.get("action") not in {"extract", "refine"}:
        return None
    if value.get("status") not in {"completed", "failed", "running"}:
        return None
    return value


def has_publish_or_stage_guard(attempt):
    return (attempt / ".staging").exists() or any(child.name.startswith(".previous-") for child in attempt.iterdir())


def protected_failed_attempts(logs, attempts, success_by_action, already_protected=None):
    already_protected = already_protected or set()
    protected = set()
    for action in ("extract", "refine"):
        success_run = success_by_action.get(action)
        success_started = attempt_started_at(logs / success_run) if success_run else None
        eligible = []
        for attempt, state in attempts:
            if attempt in already_protected:
                continue
            if state["action"] != action or state["status"] != "failed":
                continue
            started = state.get("started_at_utc")
            if not isinstance(started, str):
                continue
            if success_started is not None and started <= success_started:
                continue
            eligible.append((started, attempt))
        if eligible:
            protected.add(max(eligible)[1])
    return protected


def prune_logs(package, yes=False):
    with package_lock(package) as logs:
        extraction = read_extraction(package)
        output = read_output(package)
        success_by_action = {
            "extract": extraction.get("run_id") if extraction else None,
            "refine": output.get("run_id") if output else None,
        }
        attempts = []
        protected = set()
        for attempt in logs.iterdir():
            state = attempt_status(attempt)
            if state is None:
                continue
            attempts.append((attempt, state))
            if state["run_id"] in set(value for value in success_by_action.values() if value):
                protected.add(attempt)
            if state["status"] == "running" or has_publish_or_stage_guard(attempt):
                protected.add(attempt)
        protected |= protected_failed_attempts(logs, attempts, success_by_action, protected)
        candidates = []
        for attempt, state in attempts:
            if attempt in protected or state["status"] not in {"completed", "failed"}:
                continue
            candidates.append((attempt, state, directory_size(attempt)))
        total = sum(size for _, _, size in candidates)
        print(f"可删除历史日志：{len(candidates)} 个；预计节省 {total} bytes")
        for attempt, state, size in candidates:
            print(f"{'DELETE' if yes else 'DRY-RUN'} {state['status']} {display_action(state['action'])} {attempt.name} {size} bytes")
        if yes:
            for attempt, _, _ in candidates:
                shutil.rmtree(attempt)
        else:
            print("预览模式：添加 --yes 才会删除")


def doctor():
    for name, required in (("ffmpeg", True), ("ffprobe", True), ("course2md", True),
                           ("codex", False), ("pandoc", False)):
        path = shutil.which(name)
        print(f"{'必需' if required else '可选'} {name}: {path or '未找到'}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="./tutorial.sh",
        description="从离线视频资源包生成独立的图文教程",
        epilog=("常用示例：\n"
                "  ./tutorial.sh create \"Unity CLI 与 Codex\" --from .data/1\n"
                "  ./tutorial.sh input detect tutorials/unity-cli-与-codex --dry-run\n"
                "  ./tutorial.sh input check tutorials/unity-cli-与-codex\n"
                "  ./tutorial.sh extract tutorials/unity-cli-与-codex --dry-run\n"
                "  ./tutorial.sh generate tutorials/unity-cli-与-codex --dry-run\n"
                "  ./tutorial.sh generate tutorials/unity-cli-与-codex --overwrite --yes\n"
                "  ./tutorial.sh html tutorials/unity-cli-与-codex\n"
                "  ./tutorial.sh status tutorials/unity-cli-与-codex\n"
                "  ./tutorial.sh logs prune tutorials/unity-cli-与-codex"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create_parser = commands.add_parser(
        "create",
        help="按标题创建教程资源包，可复制离线视频、字幕、封面或音频",
        epilog="示例：./tutorial.sh create \"Unity CLI 与 Codex\" --from .data/1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    create_parser.add_argument("title")
    create_parser.add_argument("--from", dest="source", type=Path, help="复制此目录中的离线素材")

    input_parser = commands.add_parser(
        "input",
        help="检测或校验 input/material.json 与离线素材",
        epilog=("示例：\n"
                "  ./tutorial.sh input detect tutorials/unity-cli-与-codex --dry-run\n"
                "  ./tutorial.sh input check tutorials/unity-cli-与-codex"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    input_commands = input_parser.add_subparsers(dest="input_command", required=True)
    detect_parser = input_commands.add_parser(
        "detect",
        help="扫描 input/ 并更新 material.json",
        epilog=("示例：\n"
                "  ./tutorial.sh input detect tutorials/unity-cli-与-codex --dry-run\n"
                "  ./tutorial.sh input detect tutorials/unity-cli-与-codex"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    detect_parser.add_argument("package", help="标题目录名，或教程目录路径")
    detect_parser.add_argument("--dry-run", action="store_true", help="只打印检测结果，不写 material.json")
    input_check_parser = input_commands.add_parser(
        "check",
        help="只读校验素材合同和媒体文件",
        epilog="示例：./tutorial.sh input check tutorials/unity-cli-与-codex",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    input_check_parser.add_argument("package", help="标题目录名，或教程目录路径")

    extract_parser = commands.add_parser(
        "extract",
        help="从 input 生成 extracted 原始图文稿",
        epilog=("示例：\n"
                "  ./tutorial.sh extract tutorials/unity-cli-与-codex --dry-run\n"
                "  ./tutorial.sh extract tutorials/unity-cli-与-codex\n"
                "  ./tutorial.sh extract tutorials/unity-cli-与-codex --overwrite"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    extract_parser.add_argument("package", help="标题目录名，或教程目录路径")
    extract_parser.add_argument("--overwrite", action="store_true", help="验证成功后替换 extracted 结果")
    extract_parser.add_argument("--dry-run", action="store_true",
                                help="只说明提取计划，不运行 course2md，不创建日志")
    extract_parser.add_argument("--provider", choices=("coreml", "gpu", "cpu", "npu", "api"))
    extract_parser.add_argument("--asr-model", choices=("qwen3-1.7b", "qwen3-0.6b", "whisper"),
                                help="CoreML 识别模型；qwen3-0.6b 占用较少空间")

    generate_parser = commands.add_parser(
        "generate",
        help="从 extracted 分批审阅并生成 output 教程",
        epilog=("示例：\n"
                "  ./tutorial.sh generate tutorials/unity-cli-与-codex --dry-run\n"
                "  ./tutorial.sh generate tutorials/unity-cli-与-codex --yes\n"
                "  ./tutorial.sh generate tutorials/unity-cli-与-codex --overwrite --yes\n"
                "  ./tutorial.sh generate tutorials/unity-cli-与-codex --fresh --overwrite --yes"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    generate_parser.add_argument("package", help="标题目录名，或教程目录路径")
    generate_parser.add_argument("--overwrite", action="store_true", help="验证成功后替换 output 结果")
    generate_parser.add_argument("--model", help="覆盖 workflow.json 中的 Codex 模型")
    generate_parser.add_argument("--batch-size", type=int, help="每批审阅截图数；默认读取 workflow.json")
    generate_parser.add_argument("--dry-run", action="store_true",
                                 help="只打印批次计划、output 复用/替换、断点复用和新 Codex 调用数")
    generate_parser.add_argument("--yes", action="store_true",
                                 help="确认执行 dry-run 中显示的新 Codex 调用；非交互且有新调用时必需")
    generate_parser.add_argument("--fresh", action="store_true",
                                 help="忽略失败或中断 generate run 中已完成的批次，从当前 extracted 重新生成")

    html_parser = commands.add_parser(
        "html",
        help="从已有 output/tutorial.md 导出 HTML，不调用 Codex",
        epilog=("示例：\n"
                "  ./tutorial.sh html tutorials/unity-cli-与-codex --dry-run\n"
                "  ./tutorial.sh html tutorials/unity-cli-与-codex"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    html_parser.add_argument("package", help="标题目录名，或教程目录路径")
    html_parser.add_argument("--dry-run", action="store_true",
                             help="预览 HTML 导出；不运行 pandoc，不写入文件")

    status_parser = commands.add_parser(
        "status",
        help="查看资源包当前阶段、产物和日志状态",
        epilog="示例：./tutorial.sh status tutorials/unity-cli-与-codex",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    status_parser.add_argument("package", help="标题目录名，或教程目录路径")

    logs_parser = commands.add_parser(
        "logs",
        help="管理教程包日志",
        epilog="示例：./tutorial.sh logs prune tutorials/unity-cli-与-codex --dry-run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    logs_commands = logs_parser.add_subparsers(dest="logs_command", required=True)
    prune_parser = logs_commands.add_parser(
        "prune",
        help="预览或删除可安全清理的历史日志",
        epilog=("示例：\n"
                "  ./tutorial.sh logs prune tutorials/unity-cli-与-codex\n"
                "  ./tutorial.sh logs prune tutorials/unity-cli-与-codex --yes"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    prune_parser.add_argument("package", help="标题目录名，或教程目录路径")
    prune_choice = prune_parser.add_mutually_exclusive_group()
    prune_choice.add_argument("--dry-run", action="store_true", help="只预览；这是默认行为")
    prune_choice.add_argument("--yes", action="store_true", help="确认删除预览中列出的历史日志")

    commands.add_parser("doctor", help="检查本机依赖")

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            init(args.title, args.source)
        elif args.command == "input":
            package = package_path(args.package)
            if args.input_command == "detect":
                scan(package, dry_run=args.dry_run)
            else:
                check(package)
        elif args.command == "extract":
            package = package_path(args.package)
            execute_extract(package, args.provider, args.asr_model, args.overwrite, args.dry_run)
        elif args.command == "generate":
            package = package_path(args.package)
            execute_refine(package, args.model, args.batch_size, args.overwrite,
                           args.yes, args.dry_run, args.fresh)
        elif args.command == "html":
            package = package_path(args.package)
            export_html(package, args.dry_run)
        elif args.command == "status":
            package = package_path(args.package)
            status(package)
        elif args.command == "logs":
            package = package_path(args.package)
            if args.logs_command == "prune":
                prune_logs(package, yes=args.yes)
        elif args.command == "doctor":
            doctor()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
