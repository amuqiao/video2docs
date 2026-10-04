#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export UV_CACHE_DIR="${UV_CACHE_DIR:-$SCRIPT_DIR/.tools/cache/uv}"
exec uv run --locked --project "$SCRIPT_DIR" python "$SCRIPT_DIR/scripts/tutorial_workflow.py" "$@"
