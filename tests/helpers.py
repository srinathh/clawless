"""Shared test helpers and constants."""

from datetime import datetime, timezone
from pathlib import Path

from clawless.init import init_home

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOML_CONFIG = """
[claude]
max_turns = 8
max_budget_usd = 1.00

[channels.test]
sender = "test:user1"
messages = ["Hello, who are you?", "What is 2+2?", "Create a file called test.txt in your working directory with the contents 'test'. Confirm when done.", "Create a skill called 'greet' that responds with 'Hello!' when invoked. Confirm when done."]
"""


def create_test_home(prefix: str = "") -> Path:
    """Create an isolated home dir under ./data/ with test config, return the path."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    name = f"{prefix}_{ts}" if prefix else ts
    run_dir = (PROJECT_ROOT / "data" / name).resolve()
    init_home(run_dir)
    (run_dir / "clawless.toml").write_text(TOML_CONFIG)
    return run_dir


def assert_agent_responses(responses: list[dict], run_dir: Path) -> None:
    """Shared assertions for scripted test channel responses.

    Prints each response, validates basics, checks host-controlled delivery,
    and verifies the agent created test.txt in the workspace.
    """
    assert len(responses) >= 4
    for i, resp in enumerate(responses):
        print(f"\n--- Agent response {i + 1} (to: {resp['to']}) ---\n{resp['text']}\n")
        assert resp["text"], f"Response {i + 1} is empty"
        assert "not logged in" not in resp["text"].lower(), f"Agent not authenticated: {resp['text']}"
        assert resp["to"] == "test:user1"
        # Validate no single-character spam (dot-spam bug)
        assert len(resp["text"].strip()) > 1, (
            f"Response {i + 1} is trivially short: {resp['text']!r}"
        )

    # Verify agent created test.txt in workspace (third scripted message)
    test_file = run_dir / "workspace" / "test.txt"
    assert test_file.exists(), f"Agent did not create {test_file}"
    assert test_file.read_text().strip() == "test", (
        f"Expected 'test' in {test_file}, got: {test_file.read_text()!r}"
    )

    # Verify skill created in standalone dir, not plugin dir (fourth scripted message)
    skill_file = run_dir / "workspace" / ".claude" / "skills" / "greet" / "SKILL.md"
    assert skill_file.exists(), f"Agent did not create skill at {skill_file}"
    plugin_skill = run_dir / "plugin" / "skills" / "greet" / "SKILL.md"
    assert not plugin_skill.exists(), f"Agent wrongly created skill in plugin dir at {plugin_skill}"

    # Verify clawless.db was created by the store
    db_file = run_dir / "data" / "clawless.db"
    assert db_file.exists(), f"MessageStore DB not created at {db_file}"
