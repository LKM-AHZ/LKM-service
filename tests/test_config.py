import pytest

from app.core.config import Settings


def test_pulsar_url_default_empty() -> None:
    s = Settings(env="test")
    assert s.pulsar_url == ""
    assert s.message_bus_enabled is False


def test_pulsar_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LKM_PULSAR_URL", "pulsar://h:6650")
    s = Settings(env="test")
    assert s.pulsar_url == "pulsar://h:6650"
    assert s.message_bus_enabled is True
