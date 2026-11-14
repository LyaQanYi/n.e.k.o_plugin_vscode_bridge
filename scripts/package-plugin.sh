#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$REPO_ROOT/vscode_bridge"
# N.E.K.O. checkout; set NEKO_REPO or keep it as a sibling directory of this repo.
NEKO_REPO="${NEKO_REPO:-$(cd "$REPO_ROOT/../NEKO" 2>/dev/null && pwd || true)}"
if [ -z "${NEKO_REPO:-}" ] || [ ! -d "$NEKO_REPO" ]; then
  echo "N.E.K.O. repository not found. Set NEKO_REPO to your N.E.K.O. checkout." >&2
  exit 2
fi
OUT_DIR="$REPO_ROOT/dist"

mkdir -p "$OUT_DIR"
cd "$NEKO_REPO"
uv run neko-plugin build "$SRC" -o "$OUT_DIR/vscode_bridge.neko-plugin"
echo "Built $OUT_DIR/vscode_bridge.neko-plugin"
