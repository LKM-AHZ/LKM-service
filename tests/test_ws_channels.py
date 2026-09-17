"""M6.7：WS 通用推送通道（``ws:{user_id}:{channel}``）+ 幂等字段。

覆盖四层：
1. ``parse_channel`` / ``ws_channel``：命名与白名单解析（非法通道丢弃）；
2. ``ConnectionManager`` 多通道扇出：按 (user_id, channel) 过滤，互不串扰；
3. ``broker.publish``：body 带 ``event_id``/``version``，同 event_id 重推逐字段一致，
   白名单外通道不投递；
4. ``router._parse_channels``：握手 query 的通道解析与缺省兼容（旧前端不传 channels）。
"""

import json
from typing import Any

import pytest

from app.ws import broker as ws_broker
from app.ws import router as ws_router
from app.ws.manager import ConnectionManager

UPLOAD = ws_broker.CHANNEL_UPLOAD
NOTIFY = ws_broker.CHANNEL_NOTIFY


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
        assert ws_broker.ws_channel(7, UPLOAD) == "ws:7:upload"
        assert ws_broker.ws_channel(7, NOTIFY) == "ws:7:notify"

    def test_parse_channel_round_trip(self) -> None:
        assert ws_broker.parse_channel("ws:7:upload") == (7, UPLOAD)
        assert ws_broker.parse_channel("ws:7:notify") == (7, NOTIFY)

    @pytest.mark.parametrize(
        "channel",
        [
            "ws:7:evil",  # 白名单外通道
            "ws:upload:7",  # 段序错（user_id 非数字）
            "ws:7",  # 段数不足
            "ws:7:upload:extra",  # 段数超出（split(":", 2) 后第三段含冒号）
            "other:7:upload",  # 前缀不符
        ],
    )
    def test_parse_channel_rejects_invalid(self, channel: str) -> None:
        assert ws_broker.parse_channel(channel) is None


class TestMultiChannelFanout:
    async def test_channels_do_not_crosstalk(self) -> None:
        m = ConnectionManager()
        uploader, notifiee = _FakeWS(), _FakeWS()
        await m.register(7, uploader, (UPLOAD,))
        await m.register(7, notifiee, (NOTIFY,))

        await m.dispatch(7, UPLOAD, "u-frame")
        await m.dispatch(7, NOTIFY, "n-frame")

        assert uploader.sent == ["u-frame"]
        assert notifiee.sent == ["n-frame"]
        await m.close()

    async def test_single_connection_may_subscribe_many_channels(self) -> None:
        m = ConnectionManager()
        ws = _FakeWS()
        await m.register(7, ws, (UPLOAD, NOTIFY))

        await m.dispatch(7, UPLOAD, "u-frame")
        await m.dispatch(7, NOTIFY, "n-frame")

        assert ws.sent == ["u-frame", "n-frame"]
        await m.close()

    async def test_other_user_not_reached(self) -> None:
        m = ConnectionManager()
        mine, other = _FakeWS(), _FakeWS()
        await m.register(7, mine, (NOTIFY,))
        await m.register(8, other, (NOTIFY,))

        await m.dispatch(7, NOTIFY, "payload")

        assert mine.sent == ["payload"]
        assert other.sent == []
        await m.close()

    async def test_default_subscription_is_upload(self) -> None:
        """不传 channels 时按旧语义只订阅 upload（前端零改动）。"""
        m = ConnectionManager()
        ws = _FakeWS()
        await m.register(7, ws)

        await m.dispatch(7, UPLOAD, "u-frame")
        await m.dispatch(7, NOTIFY, "n-frame")

        assert ws.sent == ["u-frame"]
        await m.close()

    async def test_unregister_removes_all_channels(self) -> None:
        m = ConnectionManager()
        ws = _FakeWS()
        await m.register(7, ws, (UPLOAD, NOTIFY))

        await m.unregister(7, ws)

        await m.dispatch(7, UPLOAD, "u-frame")
        await m.dispatch(7, NOTIFY, "n-frame")
        assert ws.sent == []
        assert m._connections.get(7) is None
        await m.close()

    async def test_dead_connection_pruned_within_channel(self) -> None:
        m = ConnectionManager()
        bad, good = _FakeWS(dead=True), _FakeWS()
        await m.register(9, bad, (NOTIFY,))
        await m.register(9, good, (NOTIFY,))

        await m.dispatch(9, NOTIFY, "payload")

        assert good.sent == ["payload"]
        async with m._lock:
            live = list(m._connections.get(9, {}).get(NOTIFY, ()))
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
            7, {"event": "notification_created"}, event_id="e-1", version=3
        )

        channel, data = recorder.calls[0]
        body = json.loads(data)
        assert channel == "ws:7:notify"
        assert body["event"] == "notification_created"
        assert body["event_id"] == "e-1"
        assert body["version"] == 3

    async def test_same_event_id_republish_is_identical(
        self, recorder: _RecordingRedis
    ) -> None:
        """同 event_id 重推 → body 逐字段一致，前端可据此去重。"""
        payload = {"event": "notification_created", "notification_id": 5}
        await ws_broker.publish_notification(7, payload, event_id="e-2", version=1)
        await ws_broker.publish_notification(7, payload, event_id="e-2", version=1)

        assert recorder.calls[0] == recorder.calls[1]
        assert json.loads(recorder.calls[0][1]) == {
            "event": "notification_created",
            "notification_id": 5,
            "event_id": "e-2",
            "version": 1,
        }

    async def test_generated_event_id_differs_per_call(
        self, recorder: _RecordingRedis
    ) -> None:
        await ws_broker.publish_upload_bound(7, {"event": "upload_registered"})
        await ws_broker.publish_upload_bound(7, {"event": "upload_registered"})

        first = json.loads(recorder.calls[0][1])
        second = json.loads(recorder.calls[1][1])
        assert first["event_id"] and second["event_id"]
        assert first["event_id"] != second["event_id"]
        assert first["version"] == 0  # 未指定版本

    async def test_unknown_channel_not_published(
        self, recorder: _RecordingRedis
    ) -> None:
        await ws_broker.publish(7, "evil", {"event": "x"})

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
