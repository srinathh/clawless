"""Shared test helpers and constants."""

from datetime import datetime, timezone
from pathlib import Path

from clawless.init import init_home

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOML_CONFIG = """
[claude]
max_turns = 5
max_budget_usd = 0.50

[channels.test]
sender = "test:user1"
messages = ["Hello, who are you?", "What is 2+2?", "Use the send_message tool to send me a message saying exactly 'tool-test-ok'", "Create a file called test.txt in your working directory with the contents 'test'. Confirm when done."]
"""


def create_test_home(prefix: str = "") -> Path:
    """Create an isolated home dir under ./data/ with test config, return the path."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    name = f"{prefix}_{ts}" if prefix else ts
    run_dir = (PROJECT_ROOT / "data" / name).resolve()
    init_home(run_dir)
    (run_dir / "data" / "config.toml").write_text(TOML_CONFIG)
    return run_dir


def assert_agent_responses(responses: list[dict], run_dir: Path) -> None:
    """Shared assertions for scripted test channel responses.

    Prints each response, validates basics, checks tool usage marker,
    and verifies the agent created test.txt in the workspace.
    """
    assert len(responses) >= 4
    for i, resp in enumerate(responses):
        print(f"\n--- Agent response {i + 1} (to: {resp['to']}) ---\n{resp['text']}\n")
        assert resp["text"], f"Response {i + 1} is empty"
        assert "not logged in" not in resp["text"].lower(), f"Agent not authenticated: {resp['text']}"
        assert resp["to"] == "test:user1"

    # Verify send_message tool was used (marker text from third scripted message)
    all_text = " ".join(r["text"] for r in responses)
    assert "tool-test-ok" in all_text, (
        f"Expected 'tool-test-ok' in responses from send_message tool, "
        f"got: {[r['text'][:80] for r in responses]}"
    )

    # Verify agent created test.txt in workspace (fourth scripted message)
    test_file = run_dir / "workspace" / "test.txt"
    assert test_file.exists(), f"Agent did not create {test_file}"
    assert test_file.read_text().strip() == "test", (
        f"Expected 'test' in {test_file}, got: {test_file.read_text()!r}"
    )
