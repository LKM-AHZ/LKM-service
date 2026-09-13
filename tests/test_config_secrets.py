"""M5 7.2.3 生产密钥校验补强验收：条件字段（S3/AUTH seam）缺失即拒绝，dev 不受影响。"""

from __future__ import annotations

import pytest

from app.core.config import Settings

_SECRETS = {
    "jwt_secret": "j" * 48,
    "totp_encryption_key": "t" * 48,
    "verification_code_pepper": "v" * 48,
}


def _mk(**over: object) -> Settings:
    return Settings(_env_file=None, **{**_SECRETS, **over})  # type: ignore[arg-type]


def test_production_base_ok_without_conditional_fields() -> None:
    s = _mk(env="production")
    assert s.is_production is True


def test_production_s3_requires_keys() -> None:
    with pytest.raises(ValueError, match="s3_access_key"):
        _mk(
            env="production",
            storage_backend="s3",
            s3_access_key="",
            s3_secret_key="",
        )


def test_production_auth_seam_requires_token_when_url_set() -> None:
    with pytest.raises(ValueError, match="auth_http_token"):
        _mk(env="production", auth_http_url="http://auth:8001", auth_http_token="")


def test_dev_ignores_conditional_secrets() -> None:
    s = _mk(env="test", storage_backend="s3", auth_http_url="http://auth:8001")
    assert s.is_production is False
