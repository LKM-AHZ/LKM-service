"""无需数据库即可验证 GraphQL 只接受显式版本路径。"""

import pytest
from httpx import ASGITransport, AsyncClient

from boot.assemble import assemble

assemble()

from app.main import app  # noqa: E402


@pytest.mark.asyncio
async def test_graphql_requires_versioned_endpoint() -> None:
    query = {"query": "{ __schema { queryType { name } } }"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        versioned = await client.post("/graphql/v1", json=query)
        alias = await client.post("/graphql", json=query)
        alias_with_header = await client.post(
            "/graphql", json=query, headers={"X-API-Version": "v1"}
        )
        unknown = await client.post("/graphql/v99", json=query)

    assert versioned.status_code == 200
    assert versioned.headers["X-API-Version"] == "v1"
    assert alias.status_code == 404
    assert alias_with_header.status_code == 404
    assert "X-API-Version" not in alias.headers
    assert unknown.status_code == 404
