import pytest

from core.config import Settings


def test_pulsar_url_default_empty() -> None:
    s = Settings(env="test")
    assert s.pulsar_url == ""
    assert s.message_bus_enabled is False


def test_pulsar_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LKM_PULSAR_URL", "pulsar://h:6650")
    s = Settings(env="test")
    assert s.pulsar_url == "pulsar://h:6650"
    assert s.message_bus_enabled is True


def test_rabbitmq_selection_uses_only_rabbit_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LKM_MESSAGE_BUS", "rabbitmq")
    monkeypatch.setenv("LKM_PULSAR_URL", "pulsar://h:6650")
    monkeypatch.delenv("LKM_RABBITMQ_URL", raising=False)
    assert Settings(env="test").message_bus_enabled is False
    monkeypatch.setenv("LKM_RABBITMQ_URL", "amqp://lkm:secret@rabbitmq/")
    assert Settings(env="test").message_bus_enabled is True
