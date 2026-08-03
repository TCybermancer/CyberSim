"""
Puts the repo root (parent of scoring/) on sys.path so scoring's own
internal absolute imports (e.g. `from scoring.alerts import Alert`)
resolve correctly during test collection -- matches how it's actually
run (`python -m scoring.cli` from the repo root).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
