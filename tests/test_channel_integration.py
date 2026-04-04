"""Integration tests for the test channel.

Requires a real Claude API key (via .credentials.json or ANTHROPIC_API_KEY).
Exercises the full pipeline: config → app → agent → channel.send().
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport

from clawless.init import init_home

# Test artifacts go under ./data/<timestamp>/ so the user can inspect them.
# The data/ directory is already gitignored.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOML_CONFIG = """
[claude]
max_turns = 5
max_budget_usd = 0.50

[channels.test]
sender = "test:user1"
messages = ["Hello, who are you?", "What is 2+2?", "Use the send_message tool to send me a message saying exactly 'tool-test-ok'"]
"""


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def client():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    run_dir = (PROJECT_ROOT / "data" / ts).resolve()
    init_home(run_dir)
    (run_dir / "data" / "config.toml").write_text(TOML_CONFIG)

    # Symlink credentials from real home so the SDK can authenticate
    real_creds = Path.home() / ".claude" / ".credentials.json"
    if real_creds.is_file():
        (run_dir / ".claude" / ".credentials.json").symlink_to(real_creds)

    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(run_dir)
    try:
        from clawless.app import app

        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
    finally:
        if old_home:
            os.environ["HOME"] = old_home
        else:
            os.environ.pop("HOME", None)


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
    assert len(responses) >= 3
    for i, resp in enumerate(responses):
        print(f"\n--- Agent response {i + 1} (to: {resp['to']}) ---\n{resp['text']}\n")
        assert resp["text"]  # non-empty response from agent
        assert "not logged in" not in resp["text"].lower(), f"Agent not authenticated: {resp['text']}"
        assert resp["to"] == "test:user1"

    # Verify send_message tool was used (marker text from third scripted message)
    all_text = " ".join(r["text"] for r in responses)
    assert "tool-test-ok" in all_text, (
        f"Expected 'tool-test-ok' in responses from send_message tool, "
        f"got: {[r['text'][:80] for r in responses]}"
    )
