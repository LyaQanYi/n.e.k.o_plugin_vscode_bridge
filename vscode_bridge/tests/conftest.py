from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_REPO = Path(__file__).resolve().parents[2]
NEKO_REPO = PLUGIN_REPO.parent / "NEKO"

for path in (str(PLUGIN_REPO), str(NEKO_REPO)):
    if path not in sys.path:
        sys.path.insert(0, path)
