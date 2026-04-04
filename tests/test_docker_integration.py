"""Docker Compose integration tests for the test channel.

Builds and runs the clawless container via docker compose, feeds scripted
messages through the test channel, and verifies responses over real HTTP.

Requires Docker and either ANTHROPIC_API_KEY or ~/.claude/.credentials.json.
Skipped by default — run with: uv run pytest -m docker -v -s
"""

import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from clawless.init import init_home

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
TEST_PORT = 18791

TOML_CONFIG = """
[claude]
max_turns = 5
max_budget_usd = 0.50

[channels.test]
sender = "test:user1"
messages = ["Hello, who are you?", "What is 2+2?"]
"""


def _resolve_credentials() -> dict[str, str]:
    """Return env vars for Docker Compose auth, or skip the test."""
    env: dict[str, str] = {}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
        return env

    creds_path = Path.home() / ".claude" / ".credentials.json"
    if creds_path.is_file():
        env["CLAUDE_CREDENTIALS_FILE"] = str(creds_path)
        return env

    pytest.skip("No ANTHROPIC_API_KEY or ~/.claude/.credentials.json found")


def _compose(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a docker compose command against the project compose file."""
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(cmd, env={**os.environ, **env}, capture_output=True, text=True)


@pytest.fixture(scope="session")
def docker_service():
    """Build, start, and tear down the clawless container."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    run_dir = (PROJECT_ROOT / "data" / f"docker_{ts}").resolve()
    init_home(run_dir)
    (run_dir / "data" / "config.toml").write_text(TOML_CONFIG)

    cred_env = _resolve_credentials()
    compose_env = {
        "CLAWLESS_HOST_DIR": str(run_dir),
        "PORT": str(TEST_PORT),
        **cred_env,
    }

    # Build and start
    result = _compose("up", "-d", "--build", env=compose_env)
    if result.returncode != 0:
        pytest.fail(f"docker compose up failed:\n{result.stderr}")

    base_url = f"http://localhost:{TEST_PORT}"

    # Wait for /health (container startup + app init)
    healthy = False
    for attempt in range(60):  # up to 5 minutes
        if attempt > 0 and attempt % 2 == 0:
            elapsed = attempt * 5
            print(f"  Waiting for container health... {elapsed}s elapsed")
        try:
            r = httpx.get(f"{base_url}/health", timeout=3)
            if r.status_code == 200:
                healthy = True
                break
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
            pass
        time.sleep(5)

    if not healthy:
        logs = _compose("logs", env=compose_env)
        _compose("down", "-v", env=compose_env)
        pytest.fail(f"Container never became healthy.\nLogs:\n{logs.stdout}\n{logs.stderr}")

    yield base_url

    # Teardown
    _compose("down", "-v", env=compose_env)


@pytest.mark.docker
def test_health(docker_service):
    r = httpx.get(f"{docker_service}/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.docker
def test_scripted_messages_get_responses(docker_service):
    base_url = docker_service

    # Poll /test/status until done (up to 5 minutes)
    status = None
    for attempt in range(60):
        if attempt > 0 and attempt % 2 == 0:
            elapsed = attempt * 5
            total_responses = status["total_responses"] if status else 0
            print(f"  Waiting for test channel... {elapsed}s elapsed, {total_responses} responses so far")
        r = httpx.get(f"{base_url}/test/status", timeout=5)
        status = r.json()
        if status["done"]:
            break
        time.sleep(5)

    assert status is not None, "No status received from test channel"
    assert status["done"] is True, f"Test channel did not finish: {status}"
    assert status["error"] is None, f"Test channel error: {status['error']}"

    r = httpx.get(f"{base_url}/test/responses", timeout=5)
    responses = r.json()["responses"]
    assert len(responses) == 2
    for i, resp in enumerate(responses):
        print(f"\n--- Agent response {i + 1} (to: {resp['to']}) ---\n{resp['text']}\n")
        assert resp["text"]  # non-empty response from agent
        assert resp["to"] == "test:user1"
