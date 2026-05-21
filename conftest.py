"""Pytest configuration.

Ensures the project root is on sys.path so `from src.x import y` works
without needing to install the package in editable mode (though we do that
anyway via `uv sync`).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
