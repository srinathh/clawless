"""Integration tests for the test channel.

Requires a real Claude API key (via .credentials.json or CLAUDE__API_KEY).
Exercises the full pipeline: config → app → agent → channel.send().
"""

import asyncio
import os
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport

# Test artifacts go under ./data/<uuid>/ so the user can inspect them.
# The data/ directory is already gitignored.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _make_toml(workspace: str, data_dir: str) -> str:
    return f"""
[app]
workspace = "{workspace}"
data_dir = "{data_dir}"

[claude]
max_turns = 5
max_budget_usd = 0.50

[channels.test]
sender = "test:user1"
messages = ["Hello, who are you?", "What is 2+2?"]
"""


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def client():
    run_dir = PROJECT_ROOT / "data" / str(uuid.uuid4())
    workspace = run_dir / "workspace"
    data_dir = run_dir / "data"
    workspace.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    config_path = run_dir / "config.toml"
    config_path.write_text(_make_toml(str(workspace), str(data_dir)))

    os.environ["CONFIG_FILE"] = str(config_path)
    try:
        from clawless.app import app

        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
    finally:
        os.environ.pop("CONFIG_FILE", None)


@pytest.mark.asyncio(loop_scope="session")
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio(loop_scope="session")
async def test_scripted_messages_get_responses(client):
    # Wait for test channel to finish (poll /test/status)
    for _ in range(120):  # up to 2 minutes
        r = await client.get("/test/status")
        status = r.json()
        if status["done"]:
            break
        await asyncio.sleep(1)

    assert status["done"] is True
    assert status["error"] is None

    r = await client.get("/test/responses")
    responses = r.json()["responses"]
    assert len(responses) == 2
    for resp in responses:
        assert resp["text"]  # non-empty response from agent
        assert resp["to"] == "test:user1"
