"""bot → backend 内部文件面的单元测试（无数据库依赖）。

覆盖两件最容易悄悄坏掉的事：
1. 公开媒体 URL 的签名/过期语义——签错或不过期，等于把媒体永久公开；
2. 内部服务守卫的 fail-closed——令牌留空时该面必须关闭，而不是放行。
"""

import hashlib
import hmac
import time
import uuid

import pytest

from app.modules.files import bot_media
from core import internal_auth
from core.config import settings
from core.err import BizError


@pytest.fixture
def bot_token(monkeypatch):
    """给内部服务令牌一个确定值，并在用例结束后复原。"""
    monkeypatch.setattr(
        settings, "bot_internal_token", "unit-test-token-0123456789abcdef"
    )
    return "unit-test-token-0123456789abcdef"


def test_public_url_round_trips(bot_token):
    media_id = uuid.uuid4()
    url = bot_media.public_url(media_id)
    assert url.startswith(f"/api/v1/media/{media_id}?exp=")
    query = dict(
        part.split("=", 1) for part in url.split("?", 1)[1].split("&") if "=" in part
    )
    assert bot_media.verify_public_token(media_id, int(query["exp"]), query["sig"])


def test_signature_is_bound_to_media_id(bot_token):
    media_id = uuid.uuid4()
    other_id = uuid.uuid4()
    exp = int(time.time()) + 60
    sig = bot_media._signature(media_id, exp)
    assert not bot_media.verify_public_token(other_id, exp, sig)


def test_signature_is_bound_to_expiry(bot_token):
    media_id = uuid.uuid4()
    exp = int(time.time()) + 60
    sig = bot_media._signature(media_id, exp)
    assert not bot_media.verify_public_token(media_id, exp + 1, sig)


def test_expired_url_is_rejected(bot_token):
    media_id = uuid.uuid4()
    exp = int(time.time()) - 1
    sig = bot_media._signature(media_id, exp)
    assert not bot_media.verify_public_token(media_id, exp, sig)


def test_missing_service_token_disables_signing(monkeypatch):
    monkeypatch.setattr(settings, "bot_internal_token", "")
    media_id = uuid.uuid4()
    exp = int(time.time()) + 60
    with pytest.raises(BizError):
        bot_media.public_url(media_id)
    # 校验侧同样不能放行：拿不到密钥就没有合法签名
    assert not bot_media.verify_public_token(media_id, exp, "0" * 64)


def test_ttl_falls_back_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "bot_media_url_ttl_s", 0)
    assert bot_media.url_ttl() == bot_media._DEFAULT_URL_TTL


async def test_guard_rejects_when_token_unset(monkeypatch):
    monkeypatch.setattr(settings, "bot_internal_token", "")
    with pytest.raises(BizError):
        await internal_auth.require_bot_service("Bearer whatever")


async def test_guard_rejects_missing_or_wrong_header(bot_token):
    with pytest.raises(BizError):
        await internal_auth.require_bot_service(None)
    with pytest.raises(BizError):
        await internal_auth.require_bot_service("Basic abc")
    with pytest.raises(BizError):
        await internal_auth.require_bot_service("Bearer wrong-token")


async def test_guard_accepts_matching_bearer(bot_token):
    await internal_auth.require_bot_service(f"Bearer {bot_token}")


def test_bot_service_configured_flag(monkeypatch):
    monkeypatch.setattr(settings, "bot_internal_token", "")
    assert not internal_auth.bot_service_configured()
    monkeypatch.setattr(settings, "bot_internal_token", "x" * 32)
    assert internal_auth.bot_service_configured()


def test_signing_key_is_domain_separated(bot_token):
    """派生密钥不能等于原始令牌：否则一次令牌泄漏即可直接构造合法 URL。"""
    expected = hmac.new(bot_token.encode(), b"lkm:media-url", hashlib.sha256).digest()
    assert bot_media._signing_key() == expected
    assert expected != bot_token.encode()
