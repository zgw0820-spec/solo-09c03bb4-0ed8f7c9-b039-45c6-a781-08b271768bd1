"""HTTP-level tests for the FastAPI app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_redirects_to_docs():
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/docs"


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_openapi_is_generated():
    spec = client.get("/openapi.json").json()
    assert "/schedule" in spec["paths"]
    assert "ScheduleRequest" in spec["components"]["schemas"]


def test_example_round_trip():
    example = client.get("/example").json()
    response = client.post("/schedule", json=example)
    assert response.status_code == 200
    body = response.json()
    assert body["feasible"] is True
    assert body["min_mounts"] == 2
    assert len(body["mounts"]) == body["min_mounts"]
    assert body["files_restored"] == ["file-A", "file-B"]


def test_infeasible_returns_200_with_diagnostics():
    payload = {
        "staging_capacity": 5,
        "files": [
            {
                "id": "F",
                "shards": [
                    {"id": "missing", "size": 1, "replicas": []},
                    {"id": "big", "size": 9, "replicas": [{"tape": "T1", "position": 1}]},
                ],
            }
        ],
    }
    response = client.post("/schedule", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["feasible"] is False
    assert body["missing_shards"][0]["shard_id"] == "missing"
    assert body["capacity_issues"][0]["file_id"] == "F"
    assert body["mounts"] == []


def test_validation_of_limits_is_422():
    payload = {
        "staging_capacity": 100,
        "files": [
            {
                "id": "F",
                "shards": [
                    {"id": f"s{i}", "size": 1, "replicas": [{"tape": f"T{i}", "position": 1}]}
                    for i in range(7)
                ],
            }
        ],
    }
    assert client.post("/schedule", json=payload).status_code == 422


@pytest.mark.parametrize(
    "bad",
    [
        {"files": []},
        {"staging_capacity": 0, "files": [{"id": "F", "shards": []}]},
    ],
)
def test_structurally_invalid_requests(bad):
    assert client.post("/schedule", json=bad).status_code == 422


def test_negative_position_rejected():
    payload = {
        "staging_capacity": 10,
        "files": [
            {
                "id": "F",
                "shards": [
                    {"id": "s", "size": 1, "replicas": [{"tape": "T1", "position": -1}]}
                ],
            }
        ],
    }
    assert client.post("/schedule", json=payload).status_code == 422
