"""Integration tests for the test channel.

Requires a real Claude API key (via .credentials.json or CLAUDE__API_KEY).
Exercises the full pipeline: config → app → agent → channel.send().
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport


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
    workspace = tempfile.mkdtemp(prefix="clawless-workspace-")
    data_dir = tempfile.mkdtemp(prefix="clawless-data-")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(_make_toml(workspace, data_dir))
        f.flush()
        config_path = f.name

    os.environ["CONFIG_FILE"] = config_path
    try:
        from clawless.app import app

        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
    finally:
        os.environ.pop("CONFIG_FILE", None)
        Path(config_path).unlink(missing_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(data_dir, ignore_errors=True)


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
