import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

FAKE = Path(__file__).resolve().parent / "fake_agy.py"


@pytest.fixture
def fake_cmd():
    return [sys.executable, str(FAKE)]
