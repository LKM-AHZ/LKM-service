from unittest.mock import Mock

import pytest

from core import config
from core.storage import factory


def test_storage_factory_invalidates_on_changed_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = Mock(return_value=object())
    monkeypatch.setattr(factory, "get_storage", build)
    monkeypatch.setattr(factory, "_settings_signature", ())

    factory.get_storage_for_settings()
    factory.get_storage_for_settings()
    assert build.cache_clear.call_count == 1

    monkeypatch.setattr(
        config.settings,
        "s3_public_endpoint_url",
        config.settings.s3_public_endpoint_url + "/changed",
    )
    factory.get_storage_for_settings()
    assert build.cache_clear.call_count == 2

    style = "virtual" if config.settings.s3_addressing_style != "virtual" else "path"
    monkeypatch.setattr(config.settings, "s3_addressing_style", style)
    factory.get_storage_for_settings()
    assert build.cache_clear.call_count == 3
