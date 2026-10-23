import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _roles_seen(tmp_path, monkeypatch):
    """스케줄러가 본 역할 설정 기록(MODEL_FAMILY를 처음에만 올림)은 시험마다 새로 시작한다."""
    monkeypatch.setenv("CHIPDEPT_ROLES_SEEN", str(tmp_path / "roles_seen.json"))
