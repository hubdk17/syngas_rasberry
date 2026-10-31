"""
Pytest bootstrap.

Puts the application root on sys.path so tests import the same absolute paths
(`backend.protocol...`) that main.py uses. Without this, tests would only pass
when run from exactly the right working directory.
"""

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent

if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
