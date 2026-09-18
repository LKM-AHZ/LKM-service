"""M6.7：WS 通用推送通道（``ws:{user_id}:{channel}``）+ 幂等字段。

覆盖四层：
1. ``parse_channel`` / ``ws_channel``：命名与白名单解析（非法通道丢弃）；
2. ``ConnectionManager`` 多通道扇出：按 (user_id, channel) 过滤，互不串扰；
3. ``broker.publish``：body 带 ``event_id``/``version``，同 event_id 重推逐字段一致，
   白名单外通道不投递；
4. ``router._parse_channels``：握手 query 的通道解析与缺省兼容（旧前端不传 channels）。
"""

import json
import uuid
from typing import Any

import pytest

from app.ws import broker as ws_broker
from app.ws import router as ws_router
from app.ws.manager import ConnectionManager

UPLOAD = ws_broker.CHANNEL_UPLOAD
NOTIFY = ws_broker.CHANNEL_NOTIFY

# 稳定的 uuid7 形态用户 id（第 3 段以 7 开头、第 4 段以 8 开头）。
_UID1 = uuid.UUID("00000000-0000-7000-8000-000000000001")
_UID2 = uuid.UUID("00000000-0000-7000-8000-000000000002")
_UID3 = uuid.UUID("00000000-0000-7000-8000-000000000003")
# 通知主键（payload 透传用，uuid7 形态）。
_NOTIFICATION_ID = uuid.UUID("00000000-0000-7000-8000-000000000010")


class _FakeWS:
    def __init__(self, *, dead: bool = False) -> None:
        self.sent: list[str] = []
        self._dead = dead

    async def send_text(self, data: str) -> None:
        if self._dead:
            raise RuntimeError("connection dead")
        self.sent.append(data)


class _RecordingRedis:
    """只记录 publish 调用的假 Redis。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def publish(self, channel: str, data: str) -> int:
        self.calls.append((channel, data))
        return 1


class TestChannelNaming:
    def test_ws_channel_prefixes_user_id(self) -> None:
        assert ws_broker.ws_channel(_UID1, UPLOAD) == f"ws:{_UID1}:upload"
        assert ws_broker.ws_channel(_UID1, NOTIFY) == f"ws:{_UID1}:notify"

    def test_parse_channel_round_trip(self) -> None:
        assert ws_broker.parse_channel(f"ws:{_UID1}:upload") == (_UID1, UPLOAD)
        assert ws_broker.parse_channel(f"ws:{_UID1}:notify") == (_UID1, NOTIFY)

    @pytest.mark.parametrize(
        "channel",
        [
            f"ws:{_UID1}:evil",  # 白名单外通道
            f"ws:upload:{_UID1}",  # 段序错（user_id 段非 uuid）
            f"ws:{_UID1}",  # 段数不足
            f"ws:{_UID1}:upload:extra",  # 段数超出（split(":", 2) 后第三段含冒号）
            f"other:{_UID1}:upload",  # 前缀不符
        ],
    )
    def test_parse_channel_rejects_invalid(self, channel: str) -> None:
        assert ws_broker.parse_channel(channel) is None


class TestMultiChannelFanout:
    async def test_channels_do_not_crosstalk(self) -> None:
        m = ConnectionManager()
        uploader, notifiee = _FakeWS(), _FakeWS()
        await m.register(_UID1, uploader, (UPLOAD,))
        await m.register(_UID1, notifiee, (NOTIFY,))

        await m.dispatch(_UID1, UPLOAD, "u-frame")
        await m.dispatch(_UID1, NOTIFY, "n-frame")

        assert uploader.sent == ["u-frame"]
        assert notifiee.sent == ["n-frame"]
        await m.close()

    async def test_single_connection_may_subscribe_many_channels(self) -> None:
        m = ConnectionManager()
        ws = _FakeWS()
        await m.register(_UID1, ws, (UPLOAD, NOTIFY))

        await m.dispatch(_UID1, UPLOAD, "u-frame")
        await m.dispatch(_UID1, NOTIFY, "n-frame")

        assert ws.sent == ["u-frame", "n-frame"]
        await m.close()

    async def test_other_user_not_reached(self) -> None:
        m = ConnectionManager()
        mine, other = _FakeWS(), _FakeWS()
        await m.register(_UID1, mine, (NOTIFY,))
        await m.register(_UID2, other, (NOTIFY,))

        await m.dispatch(_UID1, NOTIFY, "payload")

        assert mine.sent == ["payload"]
        assert other.sent == []
        await m.close()

    async def test_default_subscription_is_upload(self) -> None:
        """不传 channels 时按旧语义只订阅 upload（前端零改动）。"""
        m = ConnectionManager()
        ws = _FakeWS()
        await m.register(_UID1, ws)

        await m.dispatch(_UID1, UPLOAD, "u-frame")
        await m.dispatch(_UID1, NOTIFY, "n-frame")

        assert ws.sent == ["u-frame"]
        await m.close()

    async def test_unregister_removes_all_channels(self) -> None:
        m = ConnectionManager()
        ws = _FakeWS()
        await m.register(_UID1, ws, (UPLOAD, NOTIFY))

        await m.unregister(_UID1, ws)

        await m.dispatch(_UID1, UPLOAD, "u-frame")
        await m.dispatch(_UID1, NOTIFY, "n-frame")
        assert ws.sent == []
        assert m._connections.get(_UID1) is None
        await m.close()

    async def test_dead_connection_pruned_within_channel(self) -> None:
        m = ConnectionManager()
        bad, good = _FakeWS(dead=True), _FakeWS()
        await m.register(_UID3, bad, (NOTIFY,))
        await m.register(_UID3, good, (NOTIFY,))

        await m.dispatch(_UID3, NOTIFY, "payload")

        assert good.sent == ["payload"]
        async with m._lock:
            live = list(m._connections.get(_UID3, {}).get(NOTIFY, ()))
        assert bad not in live and good in live
        await m.close()


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingRedis:
    fake = _RecordingRedis()

    async def _fake_redis() -> Any:
        return fake

    monkeypatch.setattr(ws_broker, "get_redis", _fake_redis)
    return fake


class TestPublishPayload:
    async def test_publish_carries_event_id_and_version(
        self, recorder: _RecordingRedis
    ) -> None:
        await ws_broker.publish_notification(
            _UID1, {"event": "notification_created"}, event_id="e-1", version=3
        )

        channel, data = recorder.calls[0]
        body = json.loads(data)
        assert channel == f"ws:{_UID1}:notify"
        assert body["event"] == "notification_created"
        assert body["event_id"] == "e-1"
        assert body["version"] == 3

    async def test_same_event_id_republish_is_identical(
        self, recorder: _RecordingRedis
    ) -> None:
        """同 event_id 重推 → body 逐字段一致，前端可据此去重。"""
        payload = {
            "event": "notification_created",
            "notification_id": str(_NOTIFICATION_ID),
        }
        await ws_broker.publish_notification(_UID1, payload, event_id="e-2", version=1)
        await ws_broker.publish_notification(_UID1, payload, event_id="e-2", version=1)

        assert recorder.calls[0] == recorder.calls[1]
        assert json.loads(recorder.calls[0][1]) == {
            "event": "notification_created",
            "notification_id": str(_NOTIFICATION_ID),
            "event_id": "e-2",
            "version": 1,
        }

    async def test_generated_event_id_differs_per_call(
        self, recorder: _RecordingRedis
    ) -> None:
        await ws_broker.publish_upload_bound(_UID1, {"event": "upload_registered"})
        await ws_broker.publish_upload_bound(_UID1, {"event": "upload_registered"})

        first = json.loads(recorder.calls[0][1])
        second = json.loads(recorder.calls[1][1])
        assert first["event_id"] and second["event_id"]
        assert first["event_id"] != second["event_id"]
        assert first["version"] == 0  # 未指定版本

    async def test_unknown_channel_not_published(
        self, recorder: _RecordingRedis
    ) -> None:
        await ws_broker.publish(_UID1, "evil", {"event": "x"})

        assert recorder.calls == []


class TestChannelsQuery:
    def test_default_is_upload(self) -> None:
        assert ws_router._parse_channels("") == (UPLOAD,)
        assert ws_router._parse_channels("   ") == (UPLOAD,)

    def test_multi_and_dedup(self) -> None:
        assert ws_router._parse_channels("upload,notify") == (UPLOAD, NOTIFY)
        assert ws_router._parse_channels("notify, notify ,upload") == (NOTIFY, UPLOAD)

    def test_unknown_channel_rejected(self) -> None:
        assert ws_router._parse_channels("upload,evil") is None
        assert ws_router._parse_channels("evil") is None
