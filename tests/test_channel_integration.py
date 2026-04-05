"""Integration tests for the test channel.

Requires ANTHROPIC_API_KEY.
Exercises the full pipeline: config → app → agent → channel.send().
"""

import asyncio
import os

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from dotenv import load_dotenv
from httpx import ASGITransport

from helpers import create_test_home

load_dotenv()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def client():
    run_dir = create_test_home()
    print(f"\n=== Test home: {run_dir} ===")

    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(run_dir)
    try:
        from clawless.app import app

        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
    finally:
        # Print directory tree after test run so we can see what the SDK created
        print(f"\n=== Directories created under {run_dir} ===")
        for dirpath in sorted(run_dir.rglob("*")):
            if dirpath.is_dir():
                print(f"  {dirpath.relative_to(run_dir)}/")
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
