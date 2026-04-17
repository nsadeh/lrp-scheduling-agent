from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_health():
    """Health endpoint returns ok — tested with a minimal app to avoid lifespan."""
    from api.main import health

    test_app = FastAPI()
    test_app.get("/health")(health)

    client = TestClient(test_app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
