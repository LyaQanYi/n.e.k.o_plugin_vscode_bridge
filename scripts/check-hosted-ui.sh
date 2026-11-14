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

if [ ! -d "$NEKO_REPO/frontend/plugin-manager" ]; then
  echo "N.E.K.O. repository not found at $NEKO_REPO" >&2
  echo "Set NEKO_REPO to the N.E.K.O. checkout." >&2
  exit 2
fi

if [ ! -f "$SRC/plugin.toml" ]; then
  echo "plugin.toml not found at $SRC" >&2
  exit 2
fi

TMP="$(mktemp -d "$NEKO_REPO/plugin/plugins/.vscode_bridge-check-XXXXXX")"
TMP_NAME="$(basename "$TMP")"
cleanup() {
  rm -rf "$TMP"
}
trap cleanup EXIT

cp -R "$SRC"/. "$TMP"/

npm --prefix "$NEKO_REPO/frontend/plugin-manager" run check-hosted-tsx -- "plugin/plugins/$TMP_NAME"
npm --prefix "$NEKO_REPO/frontend/plugin-manager" run check:i18n

echo "Hosted UI checks passed for $TMP_NAME"
