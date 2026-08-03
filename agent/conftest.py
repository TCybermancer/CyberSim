"""
Makes agent/ importable as flat modules (from actions.xxx import ...,
import agent) for tests under agent/tests/ -- matches how the agent is
actually run (cwd=agent/).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
