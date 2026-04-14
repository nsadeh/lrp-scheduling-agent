import pytest
from httpx import ASGITransport, AsyncClient

from api.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    # AI components report their status (disabled when keys not set in tests)
    assert "ai" in data
    assert isinstance(data["ai"]["langfuse"], bool)
    assert isinstance(data["ai"]["llm_service"], bool)
