# video2docs

![macOS Apple Silicon](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-black)
![Python 3.12](https://img.shields.io/badge/Python-3.12-blue)
![uv](https://img.shields.io/badge/dependencies-uv-purple)
![Markdown + HTML](https://img.shields.io/badge/output-Markdown%20%2B%20HTML-green)

将**离线教学视频**整理为带截图的中文教程。工作流先用 `course2md` 提取字幕与画面，再由 Codex 分批审阅并生成 Markdown；HTML 由 Pandoc 从已有 Markdown 本地导出，不消耗 Codex token。每个视频使用独立的资源包目录。

## 快速开始

当前安装入口支持 **macOS Apple Silicon**。先安装 [Homebrew](https://brew.sh/)，并准备网络连接。克隆后运行一次：

```bash
git clone https://github.com/amuqiao/video2docs.git
cd video2docs
./setup.sh
```

`setup.sh` 按需通过 Homebrew 安装 `uv`、`ffmpeg` 和 Codex CLI，并将固定版本的 `course2md` 与 Pandoc 下载到 `.tools/bin/`，校验 SHA-256 后执行 `uv sync --locked`。重复运行会复用已校验的工具。首次调用 Codex 前还需完成账号登录：

```bash
codex login
./tutorial.sh doctor
```

准备一个目录，放入**视频和对应字幕**（`.srt` 或 `.vtt`）；封面和独立音频可选。有字幕时无需下载语音识别模型。以 `/path/to/media` 为素材目录：

```bash
./tutorial.sh create "My Tutorial" --from /path/to/media
./tutorial.sh input check my-tutorial
./tutorial.sh extract my-tutorial --dry-run
./tutorial.sh extract my-tutorial
./tutorial.sh generate my-tutorial --dry-run
./tutorial.sh generate my-tutorial --yes
./tutorial.sh html my-tutorial
./tutorial.sh status my-tutorial
```

`generate --dry-run` 显示待审阅批次与新增 Codex 调用数。`generate` 会消耗 token；仅在预览确认后使用 `--yes`。更新已有 `extracted/` 或 `output/` 时，分别在 `extract` 或 `generate` 后加 `--overwrite`。`html` 可重复运行，只更新 `tutorial.html`。

## 资源包与配置

```text
tutorials/my-tutorial/
├── input/       # 离线素材和 material.json
├── extracted/   # 原始图文稿、完整时间线和提取截图
├── output/      # tutorial.md、可选 tutorial.html、assets/、用量记录
└── log/         # 运行记录与失败恢复数据
```

项目级 [workflow.json](workflow.json) 设置默认模型、每批截图数和费用估算单价；`generate --model` 与 `--batch-size` 可覆盖本次运行。估算单价需要自行核对，实际用量记录在资源包的 `output/usage.json`。

`.tools/` 存放与平台相关的大型二进制，`tutorials/` 存放用户视频和生成结果，`.models/` 存放可选语音模型。它们均不提交到 Git；克隆后由 `setup.sh` 重建工具，素材由用户自行提供。源码目前只用 Python 标准库，`uv` 锁定 Python 3.12 环境；后续 Python 依赖应通过 `uv add` 和提交 `uv.lock` 管理。

## 验证与排查

```bash
./tutorial.sh doctor
UV_CACHE_DIR=.tools/cache/uv uv run --locked python -m unittest discover -s tests -q
./tutorial.sh logs prune my-tutorial --dry-run
```

安装失败时先检查 Homebrew 写权限、网络和 `./tutorial.sh doctor` 输出。生成失败时查看该资源包的 `log/`；重试 `generate` 会复用匹配的已完成批次。命令与更多参数见 `./tutorial.sh -h`。

依赖来源：[course2md v1.7.0](https://github.com/mizorewww/course2md/releases/tag/v1.7.0)、[Pandoc 3.12](https://github.com/jgm/pandoc/releases/tag/3.12)、[uv](https://docs.astral.sh/uv/)、[Codex CLI](https://developers.openai.com/codex/cli)。
