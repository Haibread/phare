"""Health endpoint smoke test (no external dependencies)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from phare.api.app import create_app


def test_health_ok() -> None:
    client = TestClient(create_app())
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"]
    assert body["version"]
