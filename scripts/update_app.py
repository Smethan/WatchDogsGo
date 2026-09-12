"""Standalone updater, requiring only Python's standard library and Git."""
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from watchdogs.updates import update_app
try:
    update_app(root)
except Exception as exc:
    print('Update stopped:', exc, file=sys.stderr)
    raise SystemExit(1)
