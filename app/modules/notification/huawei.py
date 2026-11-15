"""HarmonyOS NEXT Push Kit v3: service-account JWT and scenario notifications."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx
import jwt

from app.modules.notification.models import NotificationToken
from app.modules.notification.repository import NotificationTokenRepository
from core.config import Settings, settings
from core.db.session import new_worker_session
from core.secrets import reveal

logger = logging.getLogger(__name__)


class HuaweiPushError(RuntimeError):
    """Provider rejected delivery; never include tokens or credentials in errors."""


def authorization(config: Settings) -> str:
    if not config.huawei_push_enabled:
        raise HuaweiPushError("Huawei Push is not configured")
    now = int(time.time())
    return jwt.encode(
        {
            "iss": config.huawei_push_sub_account,
            "aud": "https://oauth-login.cloud.huawei.com/oauth2/v3/token",
            "iat": now,
            "exp": now + 3600,
        },
        reveal(config.huawei_push_private_key),
        algorithm="PS256",
        headers={"kid": config.huawei_push_key_id, "typ": "JWT"},
    )


async def send_notification(
    client: httpx.AsyncClient,
    tokens: list[str],
    payload: dict[str, Any],
    *,
    config: Settings = settings,
) -> None:
    """Raises on HTTP, malformed responses and provider-level partial failures."""
    if not tokens:
        return
    auth = authorization(config)
    title = str(payload.get("title") or "理科迷")[:100]
    body = str(payload.get("body") or "您有一条新的通知，请打开理科迷查看。")[:256]
    for offset in range(0, len(tokens), 1000):
        response = await client.post(
            f"https://push-api.cloud.huawei.com/v3/"
            f"{config.huawei_push_project_id}/messages:send",
            headers={"Authorization": f"Bearer {auth}", "push-type": "0"},
            json={
                "payload": {
                    "notification": {
                        "category": config.huawei_push_category,
                        "title": title,
                        "body": body,
                        "clickAction": {"actionType": 0},
                    },
                },
                "target": {"token": tokens[offset : offset + 1000]},
                "pushOptions": {"testMessage": config.huawei_push_test_message},
            },
        )
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError as exc:
            raise HuaweiPushError("Huawei Push returned invalid JSON") from exc
        if not isinstance(result, dict) or result.get("code") != "80000000":
            code = result.get("code") if isinstance(result, dict) else "invalid"
            # Provider msg may contain raw tokens; expose only the result code.
            raise HuaweiPushError(f"Huawei Push rejected delivery (code={code})")


async def push_to_user(user_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Independent best-effort delivery; in-app notification remains the truth."""
    if not settings.huawei_push_enabled:
        return
    try:
        db = await new_worker_session()
        try:
            rows = await NotificationTokenRepository(db).get_many(
                NotificationToken.user_id == user_id,
                NotificationToken.platform == "huawei",
            )
            tokens = [row.token for row in rows]
        finally:
            await db.close()
        async with httpx.AsyncClient(timeout=10) as client:
            await send_notification(client, tokens, payload)
    except Exception as exc:
        # Third-party failures may echo tokens; log only exception type.
        logger.warning(
            "Huawei Push delivery failed uid=%s error=%s", user_id, type(exc).__name__
        )
