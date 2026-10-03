"""Treehole API contract: anonymous ownership and shared visibility."""

import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import MetaData, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.modules.admin.models import Report as AdminReport
from app.modules.treehole.models import (
    Bottle,
    Conversation,
    Letter,
    Message,
    Reaction,
    Report,
    Wish,
    WishLight,
)
from app.modules.treehole.router import router
from core.db.session import get_session
from core.err import BizError, resp_json


@pytest.mark.asyncio
async def test_anonymous_treehole_round_trip() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    tables = [
        m.__table__
        for m in (
            Letter,
            Reaction,
            Conversation,
            Message,
            Bottle,
            Wish,
            WishLight,
            Report,
        )
    ]
    admin_table = AdminReport.__table__.to_metadata(MetaData())
    admin_table.c.id.server_default = None  # PostgreSQL-only uuid_generate_v7()
    async with engine.begin() as conn:
        for table in tables:
            await conn.run_sync(table.create)
        await conn.run_sync(admin_table.create)

    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def test_session():
        async with sessions() as db:
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session] = test_session

    async def on_error(_request, error: BizError):
        return resp_json(error.errcode, detail=error.detail)

    app.add_exception_handler(BizError, on_error)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as alice,
        AsyncClient(transport=transport, base_url="http://test") as bob,
    ):
        a = "/api/v1/treehole"
        assert (await alice.post(f"{a}/session")).status_code == 200
        assert (await bob.post(f"{a}/session")).status_code == 200
        letter_body = {
            "content": "A shared thought",
            "category": "heart",
            "privacy": "public",
            "codename": "moon",
            "moods": [],
            "tags": [],
            "sticker": "",
            "paper": "paper",
        }
        created = await alice.post(f"{a}/letters", json=letter_body)
        assert created.status_code == 200, created.text
        letter_id = created.json()["data"]["id"]
        assert len((await bob.get(f"{a}/letters")).json()["data"]) == 1
        private = await alice.post(
            f"{a}/letters", json={**letter_body, "privacy": "self"}
        )
        assert private.status_code == 200
        scheduled = await alice.post(
            f"{a}/letters",
            json={**letter_body, "scheduledAt": int(time.time() * 1000) + 3_600_000},
        )
        assert scheduled.status_code == 200
        assert (
            len(
                (await alice.get(f"{a}/letters", params={"scope": "mine"})).json()[
                    "data"
                ]
            )
            == 3
        )
        assert len((await bob.get(f"{a}/letters")).json()["data"]) == 1
        assert (await alice.post(f"{a}/letters", json=letter_body)).status_code == 400
        assert (
            await bob.put(f"{a}/letters/{letter_id}", json=letter_body)
        ).status_code == 403
        assert (await bob.delete(f"{a}/letters/{letter_id}")).status_code == 403
        reacted = await bob.post(f"{a}/letters/{letter_id}/reactions/like")
        assert reacted.json()["data"]["letter"]["likes"] == 1
        assert (await bob.post(f"{a}/letters/{letter_id}/reactions/like")).json()[
            "data"
        ]["letter"]["likes"] == 0
        assert (await bob.post(f"{a}/letters/{letter_id}/reactions/favorite")).json()[
            "data"
        ]["active"]

        reply = await bob.post(f"{a}/letters/{letter_id}/reply", json={"text": "hello"})
        assert reply.status_code == 200, reply.text
        conv_id = reply.json()["data"]["conversationId"]
        author_view = (await alice.get(f"{a}/conversations")).json()["data"][0]
        assert author_view["messages"][0]["from"] == "peer"
        assert author_view["peerCodename"] != "moon"
        sent = await alice.post(
            f"{a}/conversations/{conv_id}/messages", json={"text": "thanks"}
        )
        assert sent.status_code == 200
        msg_id = sent.json()["data"]["id"]
        assert (
            await bob.post(f"{a}/conversations/{conv_id}/messages/{msg_id}/recall")
        ).status_code == 404
        assert (
            await alice.post(f"{a}/conversations/{conv_id}/messages/{msg_id}/recall")
        ).status_code == 200
        assert (
            await alice.post(f"{a}/conversations/{conv_id}/block")
        ).status_code == 200
        assert (
            await bob.post(f"{a}/conversations/{conv_id}/messages", json={"text": "no"})
        ).status_code == 403

        bottle = (await alice.post(f"{a}/bottles", json={"text": "at sea"})).json()[
            "data"
        ]["id"]
        assert (
            await bob.post(f"{a}/bottles/{bottle}/reply", json={"text": "found"})
        ).status_code == 200
        assert (await alice.get(f"{a}/bottles")).json()["data"][0]["reply"] == "found"
        assert (
            await bob.post(f"{a}/bottles/{bottle}/reply", json={"text": "again"})
        ).status_code == 404

        wish = (await alice.post(f"{a}/wishes", json={"text": "good day"})).json()[
            "data"
        ]["id"]
        assert (
            await bob.put(f"{a}/wishes/{wish}", json={"text": "hijack"})
        ).status_code == 404
        await bob.post(f"{a}/wishes/{wish}/light")
        await bob.post(f"{a}/wishes/{wish}/light")
        assert (await alice.get(f"{a}/wishes")).json()["data"][0]["lights"] == 1
        assert (
            await bob.post(
                f"{a}/reports",
                json={
                    "targetType": "letter",
                    "targetId": letter_id,
                    "reason": "spam",
                },
            )
        ).status_code == 200
        async with sessions() as db:
            assert await db.scalar(select(func.count(AdminReport.id))) == 1
        assert (await alice.delete(f"{a}/letters/{letter_id}")).status_code == 200
        assert (await bob.get(f"{a}/letters")).json()["data"] == []

    await engine.dispose()
