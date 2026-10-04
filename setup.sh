#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BIN="$ROOT/.tools/bin"
CACHE="$ROOT/.tools/cache/downloads"
DOWNLOAD_DIR=${VIDEO2DOCS_DOWNLOAD_DIR:-}
export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.tools/cache/uv}"

fail() { printf '错误：%s\n' "$1" >&2; exit 1; }
sha256() { shasum -a 256 "$1" | awk '{print $1}'; }

if [[ $(uname -s) != Darwin || $(uname -m) != arm64 ]]; then
  fail "当前安装入口仅支持 macOS Apple Silicon (arm64)"
fi
for tool in curl shasum unzip awk; do
  command -v "$tool" >/dev/null 2>&1 || fail "缺少基础命令：$tool"
done
command -v brew >/dev/null 2>&1 || fail "请先安装 Homebrew：https://brew.sh/"
if [[ -n "$DOWNLOAD_DIR" && ! -d "$DOWNLOAD_DIR" ]]; then
  fail "离线下载目录不存在：$DOWNLOAD_DIR"
fi

if ! command -v uv >/dev/null 2>&1; then brew install uv; fi
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  brew install ffmpeg
fi
if ! command -v codex >/dev/null 2>&1; then brew install --cask codex; fi

mkdir -p "$BIN" "$CACHE" "$UV_CACHE_DIR"

fetch_file() {
  local url=$1 target=$2 source
  if [[ -n "$DOWNLOAD_DIR" ]]; then
    source="$DOWNLOAD_DIR/${url##*/}"
    if [[ ! -f "$source" ]]; then
      printf '缺少离线文件：%s\n' "$source" >&2
      return 1
    fi
    cp "$source" "$target"
  else
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 1 --max-time 180 "$url" -o "$target"
  fi
}

download_binary() {
  local url=$1 expected=$2 target=$3 executable=$4 temporary
  if [[ -f "$target" && $(sha256 "$target") == "$expected" ]]; then
    if [[ "$executable" == yes ]]; then chmod +x "$target"; fi
    printf '复用 %s\n' "$target"
    return
  fi
  temporary=$(mktemp "$BIN/.download.XXXXXX")
  if ! fetch_file "$url" "$temporary"; then
    rm -f "$temporary"
    fail "下载失败：$url"
  fi
  if [[ $(sha256 "$temporary") != "$expected" ]]; then
    rm -f "$temporary"
    fail "下载文件校验失败：$url"
  fi
  if [[ "$executable" == yes ]]; then chmod +x "$temporary"; fi
  mv -f "$temporary" "$target"
  printf '安装 %s\n' "$target"
}

download_binary \
  "https://github.com/mizorewww/course2md/releases/download/v1.7.0/course2md-macos-arm64" \
  "0290087928d3603722c51845935e601abe5aaae3464790d2b562a2a0c20682f3" \
  "$BIN/course2md" yes
download_binary \
  "https://github.com/mizorewww/course2md/releases/download/v1.7.0/mlx-macos-arm64.metallib" \
  "24d4cfcd3ca8b15ead691e46219f35adabbea64c9f8de4eae9bf293fd8d5eb7b" \
  "$BIN/mlx.metallib" no

PANDOC_SHA="944a597887d68721af64673df44b366905f1771307bbb57615b299d0cb9245f4"
if [[ -f "$BIN/pandoc" && $(sha256 "$BIN/pandoc") == "$PANDOC_SHA" ]]; then
  chmod +x "$BIN/pandoc"
  printf '复用 %s\n' "$BIN/pandoc"
else
  archive=$(mktemp "$CACHE/.pandoc.XXXXXX")
  binary=$(mktemp "$BIN/.pandoc.XXXXXX")
  url="https://github.com/jgm/pandoc/releases/download/3.12/pandoc-3.12-arm64-macOS.zip"
  if ! fetch_file "$url" "$archive"; then
    rm -f "$archive" "$binary"
    fail "下载失败：$url"
  fi
  if [[ $(sha256 "$archive") != "f148ca09c9f36594db527a9fc988ad736290ce428f79594c50208cd1ec58b3c0" ]]; then
    rm -f "$archive" "$binary"
    fail "Pandoc 压缩包校验失败"
  fi
  if ! unzip -p "$archive" pandoc-3.12-arm64/bin/pandoc > "$binary"; then
    rm -f "$archive" "$binary"
    fail "Pandoc 解压失败"
  fi
  rm -f "$archive"
  if [[ $(sha256 "$binary") != "$PANDOC_SHA" ]]; then
    rm -f "$binary"
    fail "Pandoc 可执行文件校验失败"
  fi
  chmod +x "$binary"
  mv -f "$binary" "$BIN/pandoc"
  printf '安装 %s\n' "$BIN/pandoc"
fi

uv sync --locked --project "$ROOT"
"$ROOT/tutorial.sh" doctor
printf '\n安装完成。首次使用 generate 前请运行 codex login。\n'
