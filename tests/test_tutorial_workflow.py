import json
import io
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tutorial_workflow as workflow  # noqa: E402


class TutorialWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name, value in (("TUTORIALS", self.root / "tutorials"), ("ROOT", self.root)):
            patcher = patch.object(workflow, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        shutil.copy2(ROOT / "workflow.json", self.root / "workflow.json")
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        original_path = os.environ["PATH"]
        os.environ["PATH"] = str(self.fake_bin) + os.pathsep + original_path
        self.addCleanup(lambda: os.environ.__setitem__("PATH", original_path))
        original_cache = os.environ.pop("QWEN3_CACHE_DIR", None)
        self.addCleanup(lambda: os.environ.__setitem__("QWEN3_CACHE_DIR", original_cache)
                        if original_cache is not None else os.environ.pop("QWEN3_CACHE_DIR", None))

    def executable(self, name, content):
        path = self.fake_bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def create_media(self, subtitle=True, audio=False, cover=False):
        source = self.root / "media"
        source.mkdir()
        workflow.subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "testsrc=size=320x180:rate=2", "-t", "2", "-pix_fmt", "yuv420p", "-y",
            str(source / "clip.mp4"),
        ], check=True)
        if subtitle:
            (source / "words.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        if audio:
            workflow.subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=16000", "-t", "2", "-y", str(source / "voice.wav"),
            ], check=True)
        if cover:
            (source / "cover.png").write_bytes(b"cover")
        return source

    def install_fakes(self):
        self.executable("course2md", """#!/usr/bin/env python3
import json, os, pathlib, sys
if '--version' in sys.argv:
    print('course2md fake')
    sys.exit(0)
source = pathlib.Path(sys.argv[1])
mode = sys.argv[sys.argv.index('--transcript-source') + 1]
assert sys.argv[sys.argv.index('--formats') + 1] == 'md'
if mode == 'subtitle':
    assert source.with_suffix('.srt').is_file()
    assert '--asr-model' not in sys.argv
else:
    import subprocess
    assert pathlib.Path(os.environ['QWEN3_CACHE_DIR']).is_dir()
    assert os.environ['HF_ENDPOINT'] == 'https://hf-mirror.com'
    assert sys.argv[sys.argv.index('--asr-model') + 1] == 'qwen3-0.6b'
    result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index', '-of', 'csv=p=0', str(source)], capture_output=True, text=True, check=True)
    assert result.stdout.strip()
out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1]) / 'local' / 'recording' / 'id'
(out / 'frames').mkdir(parents=True)
count = int(os.environ.get('FAKE_COURSE_FRAME_COUNT', '2'))
for index in range(1, count + 1):
    (out / 'frames' / f'slide_{index:04d}.jpg').write_bytes(b'jpeg-' + str(index).encode())
(out / 'frames' / 'slide_0001.jpg').write_bytes(b'jpeg' + os.environ.get('FAKE_COURSE_VARIANT', '').encode())
(out / 'course.md').write_text('# recording\\n\\n[video](' + source.resolve().as_uri() + '#t=1)\\n\\n![Frame](frames/slide_0001.jpg)\\n', encoding='utf-8')
(out / 'meta.json').write_text(json.dumps({'webpage_url': str(source)}), encoding='utf-8')
speech_count = int(os.environ.get('FAKE_COURSE_SPEECH_COUNT', '2'))
if speech_count == 2:
    events = [
        {'type': 'speech', 'start': 0.0, 'end': 0.5, 'text': 'Hello'},
        {'type': 'frame', 't': 1.0, 'image': 'frames/slide_0001.jpg'},
        {'type': 'frame', 't': 1.5, 'image': 'frames/slide_0002.jpg'},
        {'type': 'speech', 'start': 1.6, 'end': 1.8, 'text': 'Next step'},
    ]
    events += [{'type': 'frame', 't': round(1.5 + (index - 2) * 0.1, 2),
                'image': f'frames/slide_{index:04d}.jpg'} for index in range(3, count + 1)]
else:
    events = [{'type': 'speech', 'start': round(index * 0.004, 3),
               'end': round(index * 0.004 + 0.002, 3),
               'text': f'Speech line {index + 1}'}
              for index in range(speech_count)]
    events += [{'type': 'frame', 't': round(0.1 + index * (1.8 / max(count, 1)), 3),
                'image': f'frames/slide_{index + 1:04d}.jpg'} for index in range(count)]
(out / 'timeline.jsonl').write_text('\\n'.join(json.dumps(event) for event in events) + '\\n', encoding='utf-8')
""")
        self.executable("codex", """#!/usr/bin/env python3
import json, os, pathlib, sys
if '--version' in sys.argv:
    print('codex fake')
    sys.exit(0)
root = pathlib.Path(sys.argv[sys.argv.index('-C') + 1])
assert sys.argv[sys.argv.index('-m') + 1] in ('gpt-6-sol', 'gpt-6-luna')
result = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
prompt = sys.argv[sys.argv.index('-o') + 2] if len(sys.argv) > sys.argv.index('-o') + 2 else ''
if '--output-schema' in sys.argv:
    context = json.loads((root / 'batch_context.json').read_text())
    if os.environ.get('FAKE_CODEX_AUDIT_PATH'):
        with open(os.environ['FAKE_CODEX_AUDIT_PATH'], 'a', encoding='utf-8') as audit:
            context = dict(context)
            context['sandbox'] = sys.argv[sys.argv.index('--sandbox') + 1]
            context['prompt'] = prompt
            audit.write(json.dumps(context) + '\\n')
    if os.environ.get('FAKE_CODEX_FAIL_BATCH') == str(context['batch_index']):
        sys.exit(8)
    images = [pathlib.Path(sys.argv[i + 1]).name for i, arg in enumerate(sys.argv) if arg == '--image']
    assert images == [pathlib.Path(frame['image']).name for frame in context['frames']]
    mode = os.environ.get('FAKE_CODEX_FRAME_IMAGE_MODE')
    def image_value(frame):
        if mode == 'workspace_absolute':
            return str((root / 'extracted' / frame['image']).resolve())
        if mode == 'external_absolute':
            return '/tmp/outside-' + pathlib.Path(frame['image']).name
        return frame['image']
    note = {'frames': [{'image': image_value(frame), 'observation': 'visible screen', 'tutorial_value': 'step'}
                       for frame in context['frames']],
            'notes_md': 'Step at ' + str(context['start_seconds']),
            'carry_forward': 'Confirmed: ' + str(context['batch_index'])}
    result.write_text(json.dumps(note), encoding='utf-8')
else:
    assert (root / 'analysis' / 'batch-0001.json').is_file()
    if os.environ.get('FAKE_CODEX_FAIL_FINAL'):
        sys.exit(9)
    image = 'cover.png' if os.environ.get('FAKE_CODEX_USE_COVER') else 'slide_0001.jpg'
    image = 'missing.jpg' if os.environ.get('FAKE_CODEX_BAD_IMAGE') else image
    title = os.environ.get('FAKE_CODEX_VARIANT', 'Tutorial')
    final_mode = os.environ.get('FAKE_CODEX_FINAL_MODE')
    if final_mode == 'intro_overview':
        markdown = (f'# {title}\\n\\n'
                    '这是一段有实质信息的概述引言，说明教程目标、使用场景、最终产物、关键操作顺序、依赖环境以及核对方式，帮助读者确认视频内容如何转化为可执行教程。\\n\\n'
                    '## 前置条件\\n- 已准备项目。\\n\\n'
                    '## 操作步骤\\n'
                    f'1. 00:00 查看关键界面。![Step](assets/{image})\\n\\n'
                    '## 命令与配置\\n```sh\\necho tutorial\\n```\\n\\n'
                    '## 常见问题\\n- 按视频核对最终界面。\\n')
    elif final_mode == 'empty_intro':
        markdown = (f'# {title}\\n\\n'
                    '   \\n\\n'
                    '## 前置条件\\n- 已准备项目。\\n\\n'
                    '## 操作步骤\\n'
                    f'1. 00:00 查看关键界面。![Step](assets/{image})\\n\\n'
                    '## 命令与配置\\n```sh\\necho tutorial\\n```\\n\\n'
                    '## 常见问题\\n- 按视频核对最终界面。\\n')
    elif os.environ.get('FAKE_CODEX_BAD_MARKDOWN'):
        markdown = f'# {title}\\n\\n![Step](assets/{image})\\n'
    else:
        markdown = (f'# {title}\\n\\n'
                    '## 视频概述\\n本教程来自离线视频素材。\\n\\n'
                    '## 前置条件\\n- 已准备项目。\\n\\n'
                    '## 操作步骤\\n'
                    f'1. 00:00 查看关键界面。![Step](assets/{image})\\n\\n'
                    '## 命令与配置\\n```sh\\necho tutorial\\n```\\n\\n'
                    '## 常见问题\\n- 按视频核对最终界面。\\n')
    if os.environ.get('FAKE_CODEX_AUDIT_PATH'):
        with open(os.environ['FAKE_CODEX_AUDIT_PATH'], 'a', encoding='utf-8') as audit:
            audit.write(json.dumps({'stage': 'final', 'sandbox': sys.argv[sys.argv.index('--sandbox') + 1],
                                    'prompt': prompt}) + '\\n')
    if os.environ.get('FAKE_CODEX_WRITE_DRAFT'):
        (root / 'output' / 'tutorial.draft.md').write_text(markdown, encoding='utf-8')
    result.write_text(markdown, encoding='utf-8')
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1000,
      'cached_input_tokens': 200, 'output_tokens': 100}}))
""")
        self.executable("pandoc", """#!/usr/bin/env python3
import pathlib, sys
if '--version' in sys.argv:
    print('pandoc fake')
    sys.exit(0)
pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('<img src="assets/slide_0001.jpg">', encoding='utf-8')
""")

    def test_video_and_subtitle_only_are_enough(self):
        self.install_fakes()
        source = self.create_media()
        package = workflow.init("My Course", source)
        self.assertEqual(package.name, "my-course")
        self.assertTrue((package / "extracted").is_dir())
        self.assertTrue((package / "output").is_dir())
        self.assertTrue((package / "log").is_dir())
        self.assertNotEqual((source / "clip.mp4").stat().st_ino,
                            (package / "input" / "clip.mp4").stat().st_ino)
        contract, assets = workflow.check(package, verbose=False)
        self.assertIsNone(contract["assets"]["audio"])
        self.assertIsNone(assets["cover"])
        result = workflow.execute_extract(package)
        self.assertEqual(result, package / "extracted")
        self.assertTrue((result / "source.md").is_file())
        self.assertTrue((result / "frames" / "slide_0001.jpg").is_file())
        self.assertTrue((result / "frames" / "slide_0002.jpg").is_file())
        self.assertIn("frames/slide_0001.jpg", (result / "source.md").read_text())
        self.assertEqual(list((package / "output").iterdir()), [])
        self.assertFalse((result / "transcript.srt").exists())
        self.assertIn("../input/clip.mp4#t=1", (result / "source.md").read_text())
        self.assertNotIn(".staging", (result / "source.md").read_text())
        self.assertFalse((result / "source-meta.json").exists())
        events = [json.loads(line) for line in (result / "timeline.jsonl").read_text().splitlines()]
        self.assertEqual(events[1]["image"], "frames/slide_0001.jpg")
        self.assertEqual(json.loads((result / "extraction.json").read_text())["status"], "ready")
        self.assertEqual(json.loads((result / "extraction.json").read_text())["video_duration_seconds"], 2.0)
        attempts = list((package / "log").glob("*/status.json"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(json.loads((result / "extraction.json").read_text())["run_id"], attempts[0].parent.name)
        self.assertEqual(json.loads(attempts[0].read_text())["run_id"], attempts[0].parent.name)
        self.assertFalse((attempts[0].parent / ".staging").exists())
        workflow.execute_extract(package)
        self.assertEqual(len(list((package / "log").glob("*/status.json"))), 1)

    def test_nested_input_video_keeps_portable_source_link(self):
        source = self.root / "source"
        source.mkdir()
        video = self.root / "input" / "clips" / "video.mp4"
        video.parent.mkdir(parents=True)
        video.touch()
        (source / "source.md").write_text(f"[video]({video.as_uri()}#t=1)\n", encoding="utf-8")
        workflow.localize_source_links(source, video, Path("/tmp/recording.mp4"),
                                       "clips/video.mp4")
        self.assertIn("../input/clips/video.mp4#t=1", (source / "source.md").read_text())

    def test_refine_is_in_place_idempotent_and_requires_overwrite_for_change(self):
        self.install_fakes()
        package = workflow.init("Course", self.create_media())
        workflow.execute_extract(package)
        original_source = (package / "extracted/source.md").read_bytes()
        result = workflow.execute_refine(package)
        self.assertFalse((result / "tutorial.html").exists())
        self.assertTrue((result / "assets" / "slide_0001.jpg").is_file())
        self.assertFalse((result / "assets" / "slide_0002.jpg").exists())
        self.assertEqual(json.loads((result / "run.json").read_text())["status"], "completed")
        first_run_id = json.loads((result / "run.json").read_text())["run_id"]
        self.assertEqual(json.loads((package / "log" / first_run_id / "status.json").read_text())["status"], "completed")
        self.assertEqual(len(list((package / "log").glob("*/status.json"))), 2)
        workflow.execute_refine(package)
        self.assertEqual(len(list((package / "log").glob("*/status.json"))), 2)
        self.assertEqual(json.loads((result / "run.json").read_text())["run_id"], first_run_id)
        with patch.dict(os.environ, {"FAKE_CODEX_VARIANT": "Ignored Without Overwrite"}):
            workflow.execute_refine(package)
        self.assertEqual(json.loads((result / "run.json").read_text())["run_id"], first_run_id)
        self.assertNotIn("Ignored Without Overwrite", (result / "tutorial.md").read_text())
        with patch.dict(os.environ, {"FAKE_CODEX_VARIANT": "Second"}):
            workflow.execute_refine(package, overwrite=True)
        self.assertIn("Second", (result / "tutorial.md").read_text())
        self.assertNotEqual(json.loads((result / "run.json").read_text())["run_id"], first_run_id)
        self.assertEqual((package / "extracted/source.md").read_bytes(), original_source)
        self.assertFalse((result / "tutorial.html").exists())
        self.assertEqual(len(list((package / "output").glob("000*"))), 0)

    def test_batches_review_every_frame_carry_context_and_record_cost(self):
        self.install_fakes()
        with patch.dict(os.environ, {"FAKE_COURSE_FRAME_COUNT": "5"}):
            package = workflow.init("Batched", self.create_media())
            workflow.execute_extract(package)
        audit = self.root / "audit.jsonl"
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit)}):
            result = workflow.execute_refine(package, model="gpt-6-luna", batch_size=2)
        batches = [json.loads(line) for line in audit.read_text().splitlines()
                   if "frames" in json.loads(line)]
        self.assertEqual([len(batch["frames"]) for batch in batches], [2, 2, 1])
        self.assertEqual(batches[0]["previous_confirmed_context"], "")
        self.assertEqual(batches[1]["previous_confirmed_context"], "Confirmed: 1")
        self.assertEqual([event["image"] for batch in batches for event in batch["frames"]],
                         [f"frames/slide_{index:04d}.jpg" for index in range(1, 6)])
        self.assertEqual(sum(len(batch["speech"]) for batch in batches), 2)
        report = json.loads((result / "usage.json").read_text())
        self.assertEqual(report["model"], "gpt-6-luna")
        self.assertEqual(report["totals"]["input_tokens"], 4000)
        self.assertEqual(report["totals"]["cached_input_tokens"], 800)
        self.assertEqual(report["totals"]["output_tokens"], 400)
        self.assertEqual(report["totals"]["estimated_usd"], "0.000528")
        self.assertEqual(report["attempt_totals"], report["totals"])
        self.assertEqual(json.loads((result / "run.json").read_text())["schema_version"], 2)
        self.assertEqual(report["run_id"], json.loads((result / "run.json").read_text())["run_id"])
        self.assertEqual(len(report["calls"]), 4)
        self.assertEqual(len(list((result / "analysis").glob("batch-*.json"))), 3)

    def test_failed_batch_resumes_completed_review(self):
        self.install_fakes()
        with patch.dict(os.environ, {"FAKE_COURSE_FRAME_COUNT": "5"}):
            package = workflow.init("Resume Batch", self.create_media())
            workflow.execute_extract(package)
        audit = self.root / "audit.jsonl"
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit), "FAKE_CODEX_FAIL_BATCH": "2"}):
            with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                workflow.execute_refine(package, batch_size=2)
        self.assertEqual(list((package / "output").iterdir()), [])
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit)}):
            workflow.execute_refine(package, batch_size=2)
        indexes = [json.loads(line)["batch_index"] for line in audit.read_text().splitlines()
                   if "batch_index" in json.loads(line)]
        self.assertEqual(indexes, [1, 2, 2, 3])
        report = json.loads((package / "output/usage.json").read_text())
        record = json.loads((package / "output/run.json").read_text())
        self.assertEqual(report["totals"]["input_tokens"], 4000)
        self.assertEqual(report["attempt_totals"]["input_tokens"], 3000)
        self.assertEqual(record["reused_batches"], [1])
        self.assertEqual(record["resumed_from"], report["calls"][0]["reused_from"])
        self.assertEqual(report["run_id"], record["run_id"])
        self.assertEqual(json.loads((package / "log" / record["run_id"] / "status.json").read_text())["resumed_from"],
                         record["resumed_from"])

    def test_interrupted_refine_marks_attempt_failed(self):
        self.install_fakes()
        package = workflow.init("Interrupted Refine", self.create_media())
        workflow.execute_extract(package)
        with patch.object(workflow, "run_codex", side_effect=KeyboardInterrupt):
            with self.assertRaisesRegex(RuntimeError, "用户中断 generate"):
                workflow.execute_refine(package)
        self.assertEqual(list((package / "output").iterdir()), [])
        attempts = [json.loads(path.read_text()) for path in (package / "log").glob("*/status.json")]
        self.assertEqual(sorted(item["status"] for item in attempts), ["completed", "failed"])

    def test_model_requires_known_price_and_batch_size_is_positive(self):
        self.install_fakes()
        package = workflow.init("Config", self.create_media())
        workflow.execute_extract(package)
        with self.assertRaisesRegex(ValueError, "缺少 unknown-model"):
            workflow.execute_refine(package, model="unknown-model")
        with self.assertRaisesRegex(ValueError, "正整数 batch_size"):
            workflow.execute_refine(package, batch_size=0)

    def test_cover_stays_in_input_until_final_tutorial_uses_it(self):
        self.install_fakes()
        package = workflow.init("With Cover", self.create_media(cover=True))
        workflow.execute_extract(package)
        self.assertFalse((package / "extracted/frames/cover.png").exists())
        with patch.dict(os.environ, {"FAKE_CODEX_USE_COVER": "1"}):
            result = workflow.execute_refine(package)
        self.assertEqual((result / "assets/cover.png").read_bytes(),
                         (package / "input/cover.png").read_bytes())
        self.assertEqual(sorted(path.name for path in (result / "assets").iterdir()), ["cover.png"])

    def test_run_full_and_overwrite_failure_preserves_old_output(self):
        self.install_fakes()
        package = workflow.init("Stable", self.create_media())
        workflow.execute_extract(package)
        result = workflow.execute_refine(package)
        first = (result / "tutorial.md").read_bytes()
        self.assertEqual(json.loads((result / "run.json").read_text())["status"], "completed")
        self.assertEqual(sorted(path.name for path in (result / "assets").iterdir()), ["slide_0001.jpg"])
        workflow.execute_extract(package)
        workflow.execute_refine(package)
        self.assertEqual(len(list((package / "log").glob("*/status.json"))), 2)
        self.executable("course2md", "#!/bin/sh\nexit 7\n")
        with self.assertRaisesRegex(RuntimeError, "提取失败"):
            workflow.execute_extract(package, overwrite=True)
        self.assertEqual((result / "tutorial.md").read_bytes(), first)
        failures = [json.loads(p.read_text()) for p in (package / "log").glob("*/status.json")]
        self.assertEqual(sum(item["status"] == "failed" for item in failures), 1)
        self.assertFalse(any((p.parent / ".staging").exists() for p in (package / "log").glob("*/status.json")))

    def test_deleted_output_is_rebuilt_without_reextracting(self):
        self.install_fakes()
        package = workflow.init("Rebuild", self.create_media())
        workflow.execute_extract(package)
        workflow.execute_refine(package)
        before = workflow.extraction_digest(package / "extracted")
        shutil.rmtree(package / "output")
        self.executable("course2md", "#!/bin/sh\nexit 7\n")
        workflow.execute_extract(package)
        result = workflow.execute_refine(package)
        self.assertTrue((result / "tutorial.md").is_file())
        self.assertEqual(workflow.extraction_digest(package / "extracted"), before)
        self.assertEqual(len(list((package / "log").glob("*/status.json"))), 3)

    def test_reextract_keeps_output_but_marks_it_stale(self):
        self.install_fakes()
        package = workflow.init("Stale", self.create_media())
        workflow.execute_extract(package)
        workflow.execute_refine(package)
        original = (package / "output/tutorial.md").read_bytes()
        with patch.dict(os.environ, {"FAKE_COURSE_VARIANT": "v2"}):
            workflow.execute_extract(package, overwrite=True)
        self.assertEqual((package / "output/tutorial.md").read_bytes(), original)
        display = io.StringIO()
        with redirect_stdout(display):
            workflow.status(package)
        self.assertIn("输出：stale", display.getvalue())
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            workflow.execute_refine(package)
        workflow.execute_refine(package, overwrite=True)
        self.assertEqual(json.loads((package / "output/run.json").read_text())["extraction_sha256"],
                         workflow.extraction_digest(package / "extracted"))

    def test_first_refine_failure_keeps_extraction(self):
        self.install_fakes()
        self.executable("codex", "#!/bin/sh\necho 'failed to initialize in-process app-server client: Operation not permitted' >&2\nexit 8\n")
        package = workflow.init("Fallback", self.create_media())
        workflow.execute_extract(package)
        with self.assertRaisesRegex(RuntimeError, "Codex CLI 初始化被系统拒绝"):
            workflow.execute_refine(package)
        self.assertEqual(json.loads((package / "extracted/extraction.json").read_text())["status"], "ready")
        self.assertEqual(list((package / "output").iterdir()), [])
        failed = [path.parent for path in (package / "log").glob("*/status.json")
                  if json.loads(path.read_text())["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        usage = json.loads((failed[0] / "usage.json").read_text())
        self.assertEqual(usage["calls"], [])
        self.assertEqual(usage["totals"]["input_tokens"], 0)
        self.assertTrue(usage["unconfirmed_billing"]["billing_unknown"])
        self.assertEqual(usage["unconfirmed_billing"]["failed_calls"][0]["tag"], "batch-0001")
        failure = json.loads((failed[0] / "codex-failures.jsonl").read_text().splitlines()[0])
        self.assertEqual(failure["failure_phase"], "client_initialization")
        self.install_fakes()
        workflow.execute_extract(package)
        result = workflow.execute_refine(package)
        self.assertTrue((result / "tutorial.md").is_file())

    def test_failed_usage_write_does_not_hide_original_refine_failure(self):
        self.install_fakes()
        self.executable("codex", "#!/bin/sh\nexit 8\n")
        package = workflow.init("Failed Usage", self.create_media())
        workflow.execute_extract(package)
        with patch.object(workflow, "write_failed_usage", side_effect=OSError("usage disk error")):
            with self.assertRaisesRegex(RuntimeError, "Codex batch-0001 失败"):
                workflow.execute_refine(package)
        failed = [json.loads(path.read_text()) for path in (package / "log").glob("*/status.json")
                  if json.loads(path.read_text())["action"] == "refine"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["status"], "failed")
        self.assertIn("Codex batch-0001 失败", failed[0]["error"])

    def test_refine_overwrite_failure_preserves_completed_output(self):
        self.install_fakes()
        package = workflow.init("Keep Output", self.create_media())
        workflow.execute_extract(package)
        workflow.execute_refine(package)
        before = (package / "output/tutorial.md").read_bytes()
        with patch.dict(os.environ, {"FAKE_CODEX_BAD_IMAGE": "1"}):
            with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                workflow.execute_refine(package, overwrite=True)
        self.assertEqual((package / "output/tutorial.md").read_bytes(), before)

    def test_codex_cannot_modify_source_or_assets(self):
        self.install_fakes()
        package = workflow.init("Protected", self.create_media())
        workflow.execute_extract(package)
        original = (package / "extracted/source.md").read_bytes()
        self.executable("codex", """#!/usr/bin/env python3
import pathlib, sys
root = pathlib.Path(sys.argv[sys.argv.index('-C') + 1])
(root / 'extracted' / 'source.md').write_text('changed', encoding='utf-8')
(root / 'output' / 'tutorial.draft.md').write_text('# Bad\\n\\n![Step](assets/slide_0001.jpg)\\n', encoding='utf-8')
""")
        with self.assertRaisesRegex(RuntimeError, "受保护"):
            workflow.execute_refine(package)
        self.assertEqual((package / "extracted/source.md").read_bytes(), original)

    def test_first_codex_failure_cannot_publish_modified_extracted_data(self):
        self.install_fakes()
        self.executable("codex", """#!/usr/bin/env python3
import pathlib, sys
root = pathlib.Path(sys.argv[sys.argv.index('-C') + 1])
(root / 'extracted' / 'source.md').write_text('changed', encoding='utf-8')
(root / 'output' / 'tutorial.draft.md').write_text('# Bad\\n\\n![Step](assets/slide_0001.jpg)\\n', encoding='utf-8')
""")
        package = workflow.init("Corrupted", self.create_media())
        workflow.execute_extract(package)
        with self.assertRaisesRegex(RuntimeError, "受保护"):
            workflow.execute_refine(package)
        self.assertEqual(list((package / "output").iterdir()), [])
        self.assertTrue((package / "extracted/source.md").is_file())
        statuses = [json.loads(path.read_text())["status"] for path in (package / "log").glob("*/status.json")]
        self.assertEqual(sorted(statuses), ["completed", "failed"])

    def test_changed_input_needs_overwrite(self):
        self.install_fakes()
        package = workflow.init("Changed", self.create_media())
        workflow.execute_extract(package)
        subtitle = package / "input" / "words.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,500\nChanged\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            workflow.execute_extract(package)
        workflow.execute_extract(package, overwrite=True)
        record = json.loads((package / "extracted/extraction.json").read_text())
        self.assertEqual(record["assets"]["subtitle"]["sha256"], workflow.hash_file(subtitle))

    def test_invalid_subtitle_time_is_rejected(self):
        package = workflow.init("Bad Time", self.create_media())
        subtitle = package / "input" / "words.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:09,000\nNo\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "字幕时间超出"):
            workflow.check(package, verbose=False)

    def test_external_audio_asr_path(self):
        self.install_fakes()
        package = workflow.init("Audio", self.create_media(subtitle=False, audio=True))
        with patch.dict(os.environ, {"HF_ENDPOINT": "https://hf-mirror.com"}):
            result = workflow.execute_extract(package, provider="coreml",
                                          asr_model="qwen3-0.6b")
        self.assertTrue((result / "source.md").is_file())
        self.assertTrue((self.root / ".models").is_dir())

    def test_missing_audio_and_subtitle_is_rejected(self):
        package = workflow.init("Silent", self.create_media(subtitle=False))
        with self.assertRaisesRegex(ValueError, "没有音轨"):
            workflow.check(package, verbose=False)

    def test_portable_package_links(self):
        self.install_fakes()
        package = workflow.init("Move", self.create_media())
        workflow.execute_extract(package)
        moved = self.root / "moved"
        shutil.copytree(package, moved)
        source = moved / "extracted" / "source.md"
        self.assertIn("../input/clip.mp4", source.read_text())
        self.assertTrue((source.parent / "../input/clip.mp4").resolve().is_file())
        workflow.validate_images(source, prefix="frames")

    def test_interrupt_records_failure_without_output(self):
        started = self.root / "course2md-started"
        self.executable("course2md", f"#!/usr/bin/env python3\nfrom pathlib import Path\nPath({str(started)!r}).touch()\nimport time\ntime.sleep(30)\n")
        package = workflow.init("Interrupted", self.create_media())
        command = (f"import os, sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
                   f"import tutorial_workflow as w; os.environ['PATH'] = {str(self.fake_bin)!r} + os.pathsep + os.environ['PATH']; "
                   f"sys.exit(w.main(['extract', {str(package)!r}]))")
        process = workflow.subprocess.Popen(
            [sys.executable, "-c", command],
            stdout=workflow.subprocess.PIPE, stderr=workflow.subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                if started.exists():
                    break
                time.sleep(0.05)
            else:
                self.fail("course2md did not start")
            process.send_signal(signal.SIGINT)
            _, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("用户中断 course2md", stderr)
            self.assertEqual(list((package / "extracted").iterdir()), [])
            records = list((package / "log").glob("*/status.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0].read_text())["status"], "failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


class RefineOutputWorkflowContractTests(unittest.TestCase):
    setUp = TutorialWorkflowTests.setUp
    executable = TutorialWorkflowTests.executable
    create_media = TutorialWorkflowTests.create_media
    install_fakes = TutorialWorkflowTests.install_fakes

    def plan_extracted(self, frames, speech):
        extracted = self.root / f"plan-{len(list(self.root.glob('plan-*')))}"
        frames_dir = extracted / "frames"
        frames_dir.mkdir(parents=True)
        for index, timestamp in enumerate(frames, start=1):
            (frames_dir / f"slide_{index:04d}.jpg").write_bytes(f"jpeg-{index}".encode())
        events = [{"type": "frame", "t": timestamp,
                   "image": f"frames/slide_{index:04d}.jpg"}
                  for index, timestamp in enumerate(frames, start=1)]
        events.extend(speech)
        (extracted / "timeline.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        return extracted

    def plan_settings(self, batch_size=8, max_chars=None, overlap=0.0):
        settings = workflow.refine_settings(batch_size_override=batch_size)
        settings["max_transcript_chars_per_batch"] = (
            max_chars if max_chars is not None
            else settings["max_transcript_chars_per_batch"]
        )
        settings["context_overlap_seconds"] = overlap
        return settings

    def batch_audit_records(self, audit):
        return [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("stage") != "final"]

    def refine_attempts(self, package, status=None):
        attempts = []
        for path in (package / "log").glob("*/status.json"):
            try:
                record = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            if record.get("action") != "refine":
                continue
            if status is not None and record.get("status") != status:
                continue
            attempts.append(path.parent)
        return sorted(attempts)

    def uncommitted_usage(self, report):
        uncommitted = report.get("uncommitted_completed_calls", {})
        calls = uncommitted.get("calls", []) if isinstance(uncommitted, dict) else uncommitted
        total = sum(call.get("usage", {}).get("input_tokens", 0) for call in calls)
        estimated = sum(float(call.get("estimated_usd", "0") or 0) for call in calls)
        return total, estimated

    def uncommitted_calls(self, report):
        uncommitted = report.get("uncommitted_completed_calls", {})
        return uncommitted.get("calls", []) if isinstance(uncommitted, dict) else uncommitted

    def attempt_time(self, name, offset_seconds=0):
        parsed = time.strptime(name[:15], "%Y%m%dT%H%M%S")
        epoch = time.mktime(parsed) + offset_seconds
        return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))

    def make_log_attempt(self, package, name, action="refine", status="failed",
                         staging=False, previous=None):
        attempt = package / "log" / name
        attempt.mkdir(parents=True, exist_ok=True)
        workflow.write_json(attempt / "status.json", {
            "run_id": name, "action": action, "status": status,
            "started_at_utc": self.attempt_time(name),
            "finished_at_utc": self.attempt_time(name, 1),
        })
        if staging:
            (attempt / ".staging").mkdir()
        if previous:
            (attempt / previous).mkdir()
        return attempt

    def rename_attempt(self, attempt, new_name):
        target = attempt.parent / new_name
        attempt.rename(target)
        status = json.loads((target / "status.json").read_text(encoding="utf-8"))
        status["run_id"] = new_name
        status["started_at_utc"] = self.attempt_time(new_name)
        status["finished_at_utc"] = self.attempt_time(new_name, 1)
        workflow.write_json(target / "status.json", status)
        return target

    def test_refine_processes_full_sixty_two_frame_plan_without_sampling(self):
        self.install_fakes()
        with patch.dict(os.environ, {"FAKE_COURSE_FRAME_COUNT": "62",
                                     "FAKE_COURSE_SPEECH_COUNT": "390"}):
            package = workflow.init("Sixty Two Frames", self.create_media())
            workflow.execute_extract(package)
        audit = self.root / "audit.jsonl"
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit)}):
            result = workflow.execute_refine(package, batch_size=8)
        batches = self.batch_audit_records(audit)
        self.assertEqual([len(batch["frames"]) for batch in batches], [8, 8, 8, 8, 8, 8, 8, 6])
        self.assertEqual(sum(len(batch["frames"]) for batch in batches), 62)
        self.assertEqual(sum(len(batch["speech"]) for batch in batches), 390)
        self.assertEqual(len(list((result / "analysis").glob("batch-*.json"))), 8)
        self.assertEqual(len(json.loads((result / "usage.json").read_text())["calls"]), 9)

    def test_batch_planning_limits_each_call_by_images_and_transcript_chars(self):
        extracted = self.plan_extracted(
            frames=[1.0, 2.0, 3.0],
            speech=[{"type": "speech", "start": 0.1, "end": 0.5, "text": "a" * 30},
                    {"type": "speech", "start": 2.1, "end": 2.5, "text": "b" * 30},
                    {"type": "speech", "start": 3.1, "end": 3.5, "text": "c" * 30}],
        )
        groups = workflow.plan_batches(extracted, self.plan_settings(batch_size=3, max_chars=50),
                                       duration=4.0)
        self.assertGreater(len(groups), 1)
        for group in groups:
            self.assertLessEqual(len(group["frames"]), 3)
            self.assertLessEqual(group["transcript_chars"], 50)

    def test_cross_boundary_speech_is_available_to_both_neighbor_batches(self):
        extracted = self.plan_extracted(
            frames=[10.0, 20.0],
            speech=[{"type": "speech", "start": 9.0, "end": 21.0,
                     "text": "跨批次解释第二张图"}],
        )
        groups = workflow.plan_batches(extracted, self.plan_settings(batch_size=1, overlap=0.0),
                                       duration=30.0)
        self.assertEqual(len(groups), 2)
        self.assertIn("跨批次解释第二张图",
                      [item["text"] for item in groups[0]["context_speech"]])
        self.assertIn("跨批次解释第二张图",
                      [item["text"] for item in groups[1]["context_speech"]])

    def test_failed_batch_checkpoint_is_atomic_and_stale_running_attempt_can_resume(self):
        self.install_fakes()
        with patch.dict(os.environ, {"FAKE_COURSE_FRAME_COUNT": "5"}):
            package = workflow.init("Atomic Resume", self.create_media())
            workflow.execute_extract(package)
        audit = self.root / "audit.jsonl"
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit), "FAKE_CODEX_FAIL_BATCH": "2"}):
            with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                workflow.execute_refine(package, batch_size=2)
        failed = [path.parent for path in (package / "log").glob("*/status.json")
                  if json.loads(path.read_text())["status"] == "failed"][-1]
        self.assertEqual([path.name for path in (failed / "batches").glob("batch-*.json")],
                         ["batch-0001.json"])
        status = json.loads((failed / "status.json").read_text(encoding="utf-8"))
        status["status"] = "running"
        workflow.write_json(failed / "status.json", status)
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit)}):
            workflow.execute_refine(package, batch_size=2)
        self.assertEqual([record["batch_index"] for record in self.batch_audit_records(audit)],
                         [1, 2, 2, 3])
        run = json.loads((package / "output/run.json").read_text())
        self.assertEqual(run["reused_batches"], [1])
        self.assertEqual(run["resumed_from"], failed.name)

    def test_changed_model_does_not_reuse_prior_failed_plan(self):
        self.install_fakes()
        with patch.dict(os.environ, {"FAKE_COURSE_FRAME_COUNT": "5"}):
            package = workflow.init("Model Mismatch", self.create_media())
            workflow.execute_extract(package)
        audit = self.root / "audit.jsonl"
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit), "FAKE_CODEX_FAIL_BATCH": "2"}):
            with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                workflow.execute_refine(package, model="gpt-6-sol", batch_size=2)
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit)}):
            workflow.execute_refine(package, model="gpt-6-luna", batch_size=2)
        self.assertEqual([record["batch_index"] for record in self.batch_audit_records(audit)],
                         [1, 2, 1, 2, 3])
        record = json.loads((package / "output/run.json").read_text())
        self.assertEqual(record["reused_batches"], [])
        self.assertIsNone(record["resumed_from"])

    def test_final_markdown_can_be_published_from_read_only_codex_result(self):
        self.install_fakes()
        package = workflow.init("Readonly Final", self.create_media())
        workflow.execute_extract(package)
        audit = self.root / "audit.jsonl"
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit),
                                     "FAKE_CODEX_RESULT_ONLY": "1"}):
            result = workflow.execute_refine(package)
        self.assertTrue((result / "tutorial.md").is_file())
        final = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()
                 if json.loads(line).get("stage") == "final"]
        self.assertEqual(final[-1]["sandbox"], "read-only")
        self.assertFalse((package / "log" / json.loads((result / "run.json").read_text())["run_id"]
                          / ".staging").exists())

    def test_bad_final_markdown_preserves_previous_output_even_with_valid_image(self):
        self.install_fakes()
        package = workflow.init("Quality Gate", self.create_media())
        workflow.execute_extract(package)
        workflow.execute_refine(package)
        before = (package / "output/tutorial.md").read_bytes()
        with patch.dict(os.environ, {"FAKE_CODEX_BAD_MARKDOWN": "1"}):
            with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                workflow.execute_refine(package, overwrite=True)
        self.assertEqual((package / "output/tutorial.md").read_bytes(), before)
        failed = [json.loads(path.read_text()) for path in (package / "log").glob("*/status.json")
                  if json.loads(path.read_text())["status"] == "failed"]
        self.assertTrue(failed)

    def test_refine_dry_run_previews_plan_without_codex_or_output(self):
        self.install_fakes()
        package = workflow.init("Dry Run", self.create_media())
        workflow.execute_extract(package)
        self.executable("codex", "#!/bin/sh\nexit 99\n")
        display = io.StringIO()
        with redirect_stdout(display):
            code = workflow.main(["generate", str(package), "--dry-run"])
        self.assertEqual(code, 0)
        preview = json.loads(display.getvalue())
        self.assertEqual(preview["frame_count"], 2)
        self.assertEqual(preview["batch_count"], 1)
        self.assertEqual(preview["planned_codex_calls"], 2)
        self.assertNotIn("calls", preview)
        self.assertNotIn("new_calls", preview)
        self.assertEqual(preview["billing_note"],
                         "dry_run_has_no_token_usage; final cost depends on Codex reported usage")
        self.assertEqual(list((package / "output").iterdir()), [])
        refine_attempts = [json.loads(path.read_text()) for path in (package / "log").glob("*/status.json")
                           if json.loads(path.read_text())["action"] == "refine"]
        self.assertEqual(refine_attempts, [])

    def test_current_workspace_absolute_frame_paths_are_normalized(self):
        self.install_fakes()
        package = workflow.init("Absolute Frames", self.create_media())
        workflow.execute_extract(package)
        with patch.dict(os.environ, {"FAKE_CODEX_FRAME_IMAGE_MODE": "workspace_absolute"}):
            result = workflow.execute_refine(package)
        checkpoint = next((package / "log" / json.loads((result / "run.json").read_text())["run_id"]
                           / "batches").glob("batch-0001.json"))
        note = json.loads(checkpoint.read_text())["note"]
        self.assertEqual([item["image"] for item in note["frames"]],
                         ["frames/slide_0001.jpg", "frames/slide_0002.jpg"])
        self.assertTrue((result / "tutorial.md").is_file())

    def test_external_absolute_frame_paths_are_rejected_and_usage_is_reported(self):
        self.install_fakes()
        package = workflow.init("External Absolute Frames", self.create_media())
        workflow.execute_extract(package)
        with patch.dict(os.environ, {"FAKE_CODEX_FRAME_IMAGE_MODE": "external_absolute"}):
            with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                workflow.execute_refine(package)
        failed = self.refine_attempts(package, status="failed")[-1]
        self.assertTrue((failed / "batch-0001.result.json").is_file())
        self.assertIn("turn.completed", (failed / "batch-0001.jsonl").read_text())
        self.assertEqual(list((failed / "batches").glob("batch-*.json")), [])
        usage = json.loads((failed / "usage.json").read_text())
        tokens, estimated = self.uncommitted_usage(usage)
        self.assertEqual(tokens, 1000)
        self.assertGreater(estimated, 0)
        self.assertEqual(list((package / "output").iterdir()), [])

    def test_retry_recovers_raw_batch_result_without_recalling_codex_batch(self):
        self.install_fakes()
        package = workflow.init("Raw Result Resume", self.create_media())
        workflow.execute_extract(package)
        audit = self.root / "audit.jsonl"
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit)}):
            with patch.object(workflow, "validate_batch_note",
                              side_effect=RuntimeError("simulated validator regression")):
                with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                    workflow.execute_refine(package)
        failed = self.refine_attempts(package, status="failed")[-1]
        self.assertTrue((failed / "batch-0001.result.json").is_file())
        self.assertIn("turn.completed", (failed / "batch-0001.jsonl").read_text())
        self.assertEqual(list((failed / "batches").glob("batch-*.json")), [])
        usage = json.loads((failed / "usage.json").read_text())
        tokens, estimated = self.uncommitted_usage(usage)
        self.assertEqual(tokens, 1000)
        self.assertGreater(estimated, 0)

        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit),
                                     "FAKE_CODEX_FAIL_BATCH": "1"}):
            result = workflow.execute_refine(package)
        batch_calls = [record["batch_index"] for record in self.batch_audit_records(audit)]
        self.assertEqual(batch_calls, [1])
        run = json.loads((result / "run.json").read_text())
        self.assertEqual(run["reused_batches"], [1])
        self.assertEqual(run["resumed_from"], failed.name)

    def test_final_intro_after_h1_can_count_as_overview(self):
        self.install_fakes()
        package = workflow.init("Intro Overview", self.create_media())
        workflow.execute_extract(package)
        with patch.dict(os.environ, {"FAKE_CODEX_FINAL_MODE": "intro_overview"}):
            result = workflow.execute_refine(package)
        tutorial = (result / "tutorial.md").read_text(encoding="utf-8")
        self.assertIn("这是一段有实质信息的概述引言", tutorial)
        self.assertNotIn("## 视频概述", tutorial)

    def test_empty_intro_without_overview_heading_is_rejected(self):
        self.install_fakes()
        package = workflow.init("Empty Intro", self.create_media())
        workflow.execute_extract(package)
        with patch.dict(os.environ, {"FAKE_CODEX_FINAL_MODE": "empty_intro"}):
            with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                workflow.execute_refine(package)
        self.assertEqual(list((package / "output").iterdir()), [])
        failed = self.refine_attempts(package, status="failed")[-1]
        self.assertTrue((failed / "final.result.md").is_file())
        self.assertIn("turn.completed", (failed / "final.jsonl").read_text())

    def test_retry_reuses_completed_final_result_after_final_validation_failure(self):
        self.install_fakes()
        package = workflow.init("Final Raw Resume", self.create_media())
        workflow.execute_extract(package)
        result = workflow.execute_refine(package)
        before = (result / "tutorial.md").read_bytes()
        audit = self.root / "final-audit.jsonl"
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit),
                                     "FAKE_CODEX_FINAL_MODE": "intro_overview"}):
            with patch.object(workflow, "validate_tutorial_structure",
                              side_effect=RuntimeError("simulated final validator regression")):
                with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                    workflow.execute_refine(package, overwrite=True)
        self.assertEqual((package / "output/tutorial.md").read_bytes(), before)
        failed = self.refine_attempts(package, status="failed")[-1]
        self.assertTrue((failed / "final.result.md").is_file())
        self.assertIn("这是一段有实质信息的概述引言",
                      (failed / "final.result.md").read_text(encoding="utf-8"))
        self.assertIn("turn.completed", (failed / "final.jsonl").read_text())
        usage = json.loads((failed / "usage.json").read_text())
        final_uncommitted = [call for call in self.uncommitted_calls(usage)
                             if call.get("stage") == "final"]
        self.assertEqual(len(final_uncommitted), 1)
        self.assertEqual(final_uncommitted[0]["usage"]["input_tokens"], 1000)

        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit),
                                     "FAKE_CODEX_FAIL_BATCH": "1",
                                     "FAKE_CODEX_FAIL_FINAL": "1"}):
            result = workflow.execute_refine(package, overwrite=True)
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["batch_index"] for record in records if "batch_index" in record], [1])
        self.assertEqual(sum(1 for record in records if record.get("stage") == "final"), 1)
        self.assertIn("这是一段有实质信息的概述引言",
                      (result / "tutorial.md").read_text(encoding="utf-8"))
        run = json.loads((result / "run.json").read_text())
        self.assertEqual(run["reused_batches"], [1])
        self.assertEqual(run["resumed_from"], failed.name)

    def test_retry_does_not_reuse_completed_final_when_a_batch_must_rerun(self):
        self.install_fakes()
        package = workflow.init("Final Needs Fresh Batch", self.create_media())
        workflow.execute_extract(package)
        audit = self.root / "fresh-final-audit.jsonl"
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit),
                                     "FAKE_CODEX_FINAL_MODE": "intro_overview"}):
            with patch.object(workflow, "validate_tutorial_structure",
                              side_effect=RuntimeError("simulated final validator regression")):
                with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                    workflow.execute_refine(package)
        failed = self.refine_attempts(package, status="failed")[-1]
        (failed / "batches" / "batch-0001.json").unlink()
        (failed / "batch-0001.result.json").unlink()
        (failed / "batch-0001.jsonl").unlink()
        self.assertTrue((failed / "final.result.md").is_file())
        self.assertIn("turn.completed", (failed / "final.jsonl").read_text())

        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit),
                                     "FAKE_CODEX_FINAL_MODE": "intro_overview"}):
            result = workflow.execute_refine(package)
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["batch_index"] for record in records if "batch_index" in record],
                         [1, 1])
        self.assertEqual(sum(1 for record in records if record.get("stage") == "final"), 2)
        self.assertIn("这是一段有实质信息的概述引言",
                      (result / "tutorial.md").read_text(encoding="utf-8"))

    def test_missing_middle_batch_forces_downstream_batches_and_final_to_rerun(self):
        self.install_fakes()
        with patch.dict(os.environ, {"FAKE_COURSE_FRAME_COUNT": "3"}):
            package = workflow.init("Middle Batch Gap", self.create_media())
            workflow.execute_extract(package)
        audit = self.root / "middle-gap-audit.jsonl"
        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit),
                                     "FAKE_CODEX_FINAL_MODE": "intro_overview"}):
            with patch.object(workflow, "validate_tutorial_structure",
                              side_effect=RuntimeError("simulated final validator regression")):
                with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                    workflow.execute_refine(package, batch_size=1)
        failed = self.refine_attempts(package, status="failed")[-1]
        for relative in ("batches/batch-0002.json", "batch-0002.result.json", "batch-0002.jsonl"):
            (failed / relative).unlink()

        with patch.dict(os.environ, {"FAKE_CODEX_AUDIT_PATH": str(audit),
                                     "FAKE_CODEX_FINAL_MODE": "intro_overview"}):
            result = workflow.execute_refine(package, batch_size=1)
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["batch_index"] for record in records if "batch_index" in record],
                         [1, 2, 3, 2, 3])
        self.assertEqual(sum(1 for record in records if record.get("stage") == "final"), 2)
        run = json.loads((result / "run.json").read_text())
        self.assertEqual(run["reused_batches"], [1])
        self.assertEqual(run["resumed_from"], failed.name)

    def test_overwrite_ignores_failed_attempts_before_current_success_but_resumes_later_failures(self):
        self.install_fakes()
        with patch.dict(os.environ, {"FAKE_COURSE_FRAME_COUNT": "3"}):
            package = workflow.init("Overwrite Resume Boundary", self.create_media())
            workflow.execute_extract(package)
        with patch.dict(os.environ, {"FAKE_CODEX_FAIL_BATCH": "2"}):
            with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                workflow.execute_refine(package, batch_size=1)
        before_success_failed = self.rename_attempt(
            self.refine_attempts(package, status="failed")[-1],
            "20000101T000000-refine-before-success",
        )
        result = workflow.execute_refine(package, batch_size=1)
        first_run = json.loads((result / "run.json").read_text())["run_id"]
        self.assertGreater(first_run, before_success_failed.name)

        with patch.dict(os.environ, {"FAKE_CODEX_FAIL_BATCH": "1"}):
            with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                workflow.execute_refine(package, batch_size=1, overwrite=True)
        self.assertEqual(json.loads((package / "output/run.json").read_text())["run_id"], first_run)

        with patch.dict(os.environ, {"FAKE_CODEX_FAIL_BATCH": "2"}):
            with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                workflow.execute_refine(package, batch_size=1, overwrite=True)
        after_success_failed = self.rename_attempt(
            sorted(attempt for attempt in self.refine_attempts(package, status="failed")
                   if (attempt / "batches" / "batch-0001.json").is_file())[-1],
            "20990101T000000-refine-after-success",
        )
        with patch.dict(os.environ, {"FAKE_CODEX_FAIL_BATCH": "1"}):
            result = workflow.execute_refine(package, batch_size=1, overwrite=True)
        run = json.loads((result / "run.json").read_text())
        self.assertEqual(run["resumed_from"], after_success_failed.name)
        self.assertEqual(run["reused_batches"], [1])

    def test_overwrite_does_not_recover_before_current_success_when_success_status_is_missing_or_bad(self):
        self.install_fakes()
        source = self.create_media()
        for mode in ("missing", "bad-json"):
            with self.subTest(mode=mode):
                with patch.dict(os.environ, {"FAKE_COURSE_FRAME_COUNT": "3"}):
                    package = workflow.init(f"Broken Success {mode}", source)
                    workflow.execute_extract(package)
                with patch.dict(os.environ, {"FAKE_CODEX_FAIL_BATCH": "2"}):
                    with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                        workflow.execute_refine(package, batch_size=1)
                failed = self.rename_attempt(
                    sorted(attempt for attempt in self.refine_attempts(package, status="failed")
                           if (attempt / "batches" / "batch-0001.json").is_file())[-1],
                    "20000101T000000-refine-before-success",
                )
                result = workflow.execute_refine(package, batch_size=1)
                current_run = json.loads((result / "run.json").read_text())["run_id"]
                success_status = package / "log" / current_run / "status.json"
                if mode == "missing":
                    success_status.unlink()
                else:
                    success_status.write_text("{broken", encoding="utf-8")

                with patch.dict(os.environ, {"FAKE_CODEX_FAIL_BATCH": "1"}):
                    with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                        workflow.execute_refine(package, batch_size=1, overwrite=True)
                status = json.loads((self.refine_attempts(package, status="failed")[-1]
                                     / "status.json").read_text())
                self.assertNotEqual(status.get("resume_candidate"), failed.name)
                self.assertNotEqual(status.get("resumed_from"), failed.name)
                self.assertEqual(json.loads((package / "output/run.json").read_text())["run_id"],
                                 current_run)

    def test_logs_prune_previews_then_deletes_only_safe_candidates(self):
        self.install_fakes()
        package = workflow.init("Prune Logs", self.create_media())
        workflow.execute_extract(package)
        workflow.execute_refine(package)
        output_run = json.loads((package / "output/run.json").read_text())["run_id"]
        extraction_run = json.loads((package / "extracted/extraction.json").read_text())["run_id"]
        old_failed = self.make_log_attempt(package, "20000101T000000-refine-oldfailed", status="failed")
        old_completed = self.make_log_attempt(package, "20000102T000000-refine-oldcompleted", status="completed")
        latest_failed = self.make_log_attempt(package, "20990101T000000-refine-latestfailed", status="failed")
        running = self.make_log_attempt(package, "20990102T000000-refine-running", status="running",
                                        staging=True)
        previous = self.make_log_attempt(package, "20980103T000000-refine-previous", status="failed",
                                         previous=".previous-output")

        preview = io.StringIO()
        with redirect_stdout(preview):
            code = workflow.main(["logs", "prune", str(package)])
        self.assertEqual(code, 0)
        self.assertIn("可删除历史日志：2 个", preview.getvalue())
        for attempt in (old_failed, old_completed, latest_failed, running, previous,
                        package / "log" / output_run, package / "log" / extraction_run):
            with self.subTest(preview_keeps=attempt.name):
                self.assertTrue(attempt.exists())

        deleted = io.StringIO()
        with redirect_stdout(deleted):
            code = workflow.main(["logs", "prune", str(package), "--yes"])
        self.assertEqual(code, 0)
        self.assertFalse(old_failed.exists())
        self.assertFalse(old_completed.exists())
        for attempt in (latest_failed, running, previous,
                        package / "log" / output_run, package / "log" / extraction_run):
            with self.subTest(delete_keeps=attempt.name):
                self.assertTrue(attempt.exists())


class CliWorkflowContractTests(unittest.TestCase):
    setUp = TutorialWorkflowTests.setUp
    tearDown = TutorialWorkflowTests.tearDown if hasattr(TutorialWorkflowTests, "tearDown") else lambda self: None
    executable = TutorialWorkflowTests.executable
    create_media = TutorialWorkflowTests.create_media
    install_fakes = TutorialWorkflowTests.install_fakes
    refine_attempts = RefineOutputWorkflowContractTests.refine_attempts

    def invoke_main(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = None
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                result = workflow.main(argv)
                code = 0 if result is None else result
            except SystemExit as error:
                code = 0 if error.code is None else error.code
        return code, stdout.getvalue(), stderr.getvalue()

    def log_attempt_dirs(self, package):
        return sorted(path for path in (package / "log").iterdir() if path.is_dir())

    def parse_json_stdout(self, stdout):
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as error:
            self.fail(f"stdout 不是 JSON：{stdout!r}; {error}")

    def build_extracted_package(self, title="Generate Dry", frame_count=3):
        self.install_fakes()
        source = self.root / ("media-" + workflow.slugify(title))
        source.mkdir()
        workflow.subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "testsrc=size=320x180:rate=2", "-t", "2", "-pix_fmt", "yuv420p", "-y",
            str(source / "clip.mp4"),
        ], check=True)
        (source / "words.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        with patch.dict(os.environ, {"FAKE_COURSE_FRAME_COUNT": str(frame_count)}):
            package = workflow.init(title, source)
            workflow.execute_extract(package)
        return package

    def build_completed_output_package(self, title="HTML Export", frame_count=2):
        package = self.build_extracted_package(title, frame_count=frame_count)
        workflow.execute_refine(package)
        return package

    def test_main_help_highlights_primary_commands_and_rejects_legacy_commands(self):
        code, stdout, stderr = self.invoke_main(["--help"])
        self.assertEqual((code, stderr), (0, ""))
        for text in ("create", "input", "detect", "check", "extract", "generate", "html",
                     "status", "doctor", "logs", "./tutorial.sh"):
            with self.subTest(help_text=text):
                self.assertIn(text, stdout)
        self.assertRegex(stdout, r"示例|Examples")
        for legacy in ("init", "scan", "check", "refine", "run"):
            with self.subTest(legacy=legacy):
                self.assertNotRegex(stdout, rf"(?m)^\s*{legacy}\b")
                code, _, stderr = self.invoke_main([legacy, "--help"])
                self.assertEqual(code, 2)
                self.assertIn("invalid choice", stderr)

    def test_key_subcommand_help_shows_examples_and_yes_semantics(self):
        for argv in (["create", "--help"], ["input", "--help"],
                     ["input", "detect", "--help"], ["input", "check", "--help"],
                     ["extract", "--help"], ["generate", "--help"], ["html", "--help"],
                     ["logs", "prune", "--help"]):
            with self.subTest(argv=argv):
                code, stdout, stderr = self.invoke_main(argv)
                self.assertEqual((code, stderr), (0, ""))
                self.assertRegex(stdout, r"示例|Examples|usage")
        code, stdout, stderr = self.invoke_main(["generate", "--help"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("--yes", stdout)
        self.assertRegex(stdout, r"消耗 token|Codex 调用")
        self.assertRegex(stdout, r"非交互|确认")
        self.assertNotIn("--html", stdout)

        code, stdout, stderr = self.invoke_main(["html", "--help"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("--dry-run", stdout)
        self.assertRegex(stdout, r"pandoc|HTML")

    def test_html_command_exports_locally_and_is_idempotent(self):
        package = self.build_completed_output_package()
        output = package / "output"
        before = {name: (output / name).read_bytes()
                  for name in ("tutorial.md", "run.json", "usage.json")}
        before_logs = self.log_attempt_dirs(package)
        self.executable("codex", "#!/bin/sh\necho codex must not run >&2\nexit 99\n")

        code, stdout, stderr = self.invoke_main(["html", str(package)])
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue((output / "tutorial.html").is_file())
        self.assertIn('src="assets/slide_0001.jpg"', (output / "tutorial.html").read_text())
        self.assertEqual({name: (output / name).read_bytes()
                          for name in ("tutorial.md", "run.json", "usage.json")}, before)
        self.assertEqual(self.log_attempt_dirs(package), before_logs)

        (output / "tutorial.html").write_text("stale html", encoding="utf-8")
        code, stdout, stderr = self.invoke_main(["html", str(package)])
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn('src="assets/slide_0001.jpg"', (output / "tutorial.html").read_text())
        self.assertEqual({name: (output / name).read_bytes()
                          for name in ("tutorial.md", "run.json", "usage.json")}, before)
        self.assertEqual(self.log_attempt_dirs(package), before_logs)

    def test_html_dry_run_reports_plan_without_pandoc_or_side_effects(self):
        package = self.build_completed_output_package("HTML Dry")
        output = package / "output"
        before = {name: (output / name).read_bytes()
                  for name in ("tutorial.md", "run.json", "usage.json")}
        before_logs = self.log_attempt_dirs(package)

        real_which = workflow.shutil.which
        with patch.object(workflow.shutil, "which",
                          side_effect=lambda name: None if name == "pandoc" else real_which(name)):
            code, stdout, stderr = self.invoke_main(["html", str(package), "--dry-run"])
        self.assertEqual((code, stderr), (0, ""))
        preview = self.parse_json_stdout(stdout)
        self.assertEqual(preview["operation"], "html")
        self.assertEqual(Path(preview["source"]).resolve(), (output / "tutorial.md").resolve())
        self.assertEqual(Path(preview["target"]).resolve(), (output / "tutorial.html").resolve())
        self.assertEqual(preview["action"], "create")
        self.assertFalse(preview["pandoc_available"])
        self.assertEqual(preview["codex_calls"], 0)
        self.assertFalse(preview["dry_run_has_side_effects"])
        self.assertFalse((output / "tutorial.html").exists())
        self.assertEqual({name: (output / name).read_bytes()
                          for name in ("tutorial.md", "run.json", "usage.json")}, before)
        self.assertEqual(self.log_attempt_dirs(package), before_logs)

    def test_html_export_failure_preserves_existing_html_and_metadata(self):
        package = self.build_completed_output_package("HTML Failure")
        output = package / "output"
        (output / "tutorial.html").write_text("old html", encoding="utf-8")
        before = {name: (output / name).read_bytes()
                  for name in ("tutorial.md", "run.json", "usage.json", "tutorial.html")}
        before_logs = self.log_attempt_dirs(package)

        real_which = workflow.shutil.which
        with patch.object(workflow.shutil, "which",
                          side_effect=lambda name: None if name == "pandoc" else real_which(name)):
            code, stdout, stderr = self.invoke_main(["html", str(package)])
        self.assertEqual(code, 1)
        self.assertIn("pandoc", stderr)
        self.assertEqual({name: (output / name).read_bytes()
                          for name in ("tutorial.md", "run.json", "usage.json", "tutorial.html")}, before)
        self.assertEqual(self.log_attempt_dirs(package), before_logs)

        self.executable("pandoc", """#!/usr/bin/env python3
import pathlib, sys
target = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
target.write_text('broken html', encoding='utf-8')
sys.exit(7)
""")
        code, stdout, stderr = self.invoke_main(["html", str(package)])
        self.assertEqual(code, 1)
        self.assertIn("pandoc", stderr)
        self.assertEqual({name: (output / name).read_bytes()
                          for name in ("tutorial.md", "run.json", "usage.json", "tutorial.html")}, before)
        self.assertEqual(self.log_attempt_dirs(package), before_logs)

    def test_legacy_generate_html_flag_is_rejected(self):
        package = self.build_extracted_package("Legacy HTML Flag")
        code, stdout, stderr = self.invoke_main(["generate", str(package), "--html", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments", stderr)
        self.assertEqual(list((package / "output").iterdir()), [])

    def test_input_detect_dry_run_does_not_modify_material_contract(self):
        source = self.create_media(cover=True)
        package = workflow.init("Input Dry")
        for media in source.iterdir():
            shutil.copy2(media, package / "input" / media.name)
        material = package / "input/material.json"
        before = material.read_bytes()

        code, stdout, stderr = self.invoke_main(["input", "detect", str(package), "--dry-run"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(material.read_bytes(), before)
        self.assertIn("clip.mp4", stdout)
        self.assertIn("words.srt", stdout)
        self.assertIn("cover.png", stdout)

    def test_extract_dry_run_reports_reuse_or_replace_without_course2md_or_logs(self):
        self.install_fakes()
        package = workflow.init("Extract Dry", self.create_media())
        self.executable("course2md", "#!/bin/sh\necho course2md must not run >&2\nexit 99\n")

        code, stdout, stderr = self.invoke_main(["extract", str(package), "--dry-run"])
        self.assertEqual((code, stderr), (0, ""))
        preview = self.parse_json_stdout(stdout)
        self.assertEqual(preview["operation"], "extract")
        self.assertEqual(preview["action"], "create")
        self.assertTrue(preview["will_run_course2md"])
        self.assertFalse(preview["dry_run_has_side_effects"])
        self.assertEqual(self.log_attempt_dirs(package), [])
        self.assertEqual(list((package / "extracted").iterdir()), [])

        self.install_fakes()
        workflow.execute_extract(package)
        before_logs = self.log_attempt_dirs(package)
        self.executable("course2md", "#!/bin/sh\necho course2md must not run >&2\nexit 99\n")
        code, stdout, stderr = self.invoke_main(["extract", str(package), "--dry-run"])
        self.assertEqual((code, stderr), (0, ""))
        preview = self.parse_json_stdout(stdout)
        self.assertEqual(preview["action"], "reuse")
        self.assertFalse(preview["will_run_course2md"])
        self.assertEqual(self.log_attempt_dirs(package), before_logs)

        code, stdout, stderr = self.invoke_main(["extract", str(package), "--dry-run", "--overwrite"])
        self.assertEqual((code, stderr), (0, ""))
        preview = self.parse_json_stdout(stdout)
        self.assertEqual(preview["action"], "replace")
        self.assertTrue(preview["will_run_course2md"])
        self.assertEqual(self.log_attempt_dirs(package), before_logs)

    def test_generate_dry_run_reports_new_calls_and_recovery_without_side_effects(self):
        package = self.build_extracted_package(frame_count=3)
        self.executable("codex", "#!/bin/sh\necho codex must not run >&2\nexit 99\n")

        code, stdout, stderr = self.invoke_main(["generate", str(package), "--batch-size", "1", "--dry-run"])
        self.assertEqual((code, stderr), (0, ""))
        preview = self.parse_json_stdout(stdout)
        self.assertEqual(preview["planned_codex_calls"], 4)
        self.assertNotIn("calls", preview)
        self.assertNotIn("new_calls", preview)
        self.assertEqual(preview["new_codex_calls"], 4)
        self.assertEqual(preview["reused_contiguous_batches"], [])
        self.assertTrue(preview["final_call_needed"])
        self.assertEqual([path.name for path in self.log_attempt_dirs(package)],
                         [json.loads((package / "extracted/extraction.json").read_text())["run_id"]])
        self.assertEqual(list((package / "output").iterdir()), [])

        self.install_fakes()
        with patch.dict(os.environ, {"FAKE_CODEX_FAIL_BATCH": "2"}):
            with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                workflow.execute_refine(package, batch_size=1)
        partial_failed = self.refine_attempts(package, status="failed")[-1]
        self.executable("codex", "#!/bin/sh\necho codex must not run >&2\nexit 99\n")
        code, stdout, stderr = self.invoke_main(["generate", str(package), "--batch-size", "1", "--dry-run"])
        self.assertEqual((code, stderr), (0, ""))
        preview = self.parse_json_stdout(stdout)
        self.assertEqual(preview["planned_codex_calls"], 4)
        self.assertNotIn("calls", preview)
        self.assertNotIn("new_calls", preview)
        self.assertEqual(preview["new_codex_calls"], 3)
        self.assertEqual(preview["reused_contiguous_batches"], [1])
        self.assertTrue(preview["final_call_needed"])
        self.assertEqual(self.refine_attempts(package, status="failed")[-1], partial_failed)

        self.install_fakes()
        with patch.dict(os.environ, {"FAKE_CODEX_FINAL_MODE": "intro_overview"}):
            with patch.object(workflow, "validate_tutorial_structure",
                              side_effect=RuntimeError("simulated final validator regression")):
                with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                    workflow.execute_refine(package, batch_size=1)
        full_failed = self.refine_attempts(package, status="failed")[-1]
        self.executable("codex", "#!/bin/sh\necho codex must not run >&2\nexit 99\n")
        code, stdout, stderr = self.invoke_main(["generate", str(package), "--batch-size", "1", "--dry-run"])
        self.assertEqual((code, stderr), (0, ""))
        preview = self.parse_json_stdout(stdout)
        self.assertEqual(preview["planned_codex_calls"], 4)
        self.assertNotIn("calls", preview)
        self.assertNotIn("new_calls", preview)
        self.assertEqual(preview["new_codex_calls"], 0)
        self.assertEqual(preview["reused_contiguous_batches"], [1, 2, 3])
        self.assertFalse(preview["final_call_needed"])
        self.assertEqual(self.refine_attempts(package, status="failed")[-1], full_failed)
        self.assertEqual(list((package / "output").iterdir()), [])

    def test_generate_requires_yes_only_when_paid_calls_are_pending(self):
        pending = self.build_extracted_package("Generate Confirm Pending", frame_count=2)
        code, stdout, stderr = self.invoke_main(["generate", str(pending), "--batch-size", "1"])
        self.assertEqual(code, 1)
        self.assertIn("--yes", stderr)
        self.assertEqual(list((pending / "output").iterdir()), [])

        reusable = self.build_extracted_package("Generate Confirm Reuse", frame_count=2)
        with patch.dict(os.environ, {"FAKE_CODEX_FINAL_MODE": "intro_overview"}):
            with patch.object(workflow, "validate_tutorial_structure",
                              side_effect=RuntimeError("simulated final validator regression")):
                with self.assertRaisesRegex(RuntimeError, "generate 失败"):
                    workflow.execute_refine(reusable, batch_size=1)
        with patch.dict(os.environ, {"FAKE_CODEX_FAIL_BATCH": "1", "FAKE_CODEX_FAIL_FINAL": "1"}):
            code, stdout, stderr = self.invoke_main(["generate", str(reusable), "--batch-size", "1"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue((reusable / "output/tutorial.md").is_file())
        run = json.loads((reusable / "output/run.json").read_text())
        self.assertEqual(run["reused_batches"], [1, 2])


if __name__ == "__main__":
    unittest.main()
