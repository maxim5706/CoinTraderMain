import os
import sys
from pathlib import Path

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def clear_test_cooldowns():
    """Clear cooldown files before each test to avoid test pollution."""
    cooldown_files = [
        Path("data/paper_cooldowns.json"),
        Path("data/live_cooldowns.json"),
    ]
    for f in cooldown_files:
        if f.exists():
            f.unlink()
    yield
    # Cleanup after test too
    for f in cooldown_files:
        if f.exists():
            f.unlink()
