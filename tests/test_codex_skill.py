"""The generic Codex integration skill (integrations/codex-skill/) — content guards (offline).

Pure file checks: assert the AGENTS.md policy declares the trigger phrases and steers Codex through
the loopeng workflow. Guards against drift; needs no Codex/loopeng install.
"""
import pathlib

SKILL = pathlib.Path(__file__).resolve().parents[1] / "integrations" / "codex-skill"
AGENTS = SKILL / "AGENTS.md"


def test_codex_skill_files_exist():
    assert AGENTS.is_file()
    assert (SKILL / "README.md").is_file()


def test_codex_skill_declares_the_trigger_phrases():
    text = AGENTS.read_text()
    for trigger in ["run a verify loop", "fix until tests pass", "跑闭环"]:
        assert trigger in text, f"missing trigger phrase: {trigger!r}"


def test_codex_skill_steers_the_loopeng_workflow():
    text = AGENTS.read_text()
    # the workflow the policy must drive Codex through (phrases that encode the requirement,
    # not bare tokens that could match incidental words)
    for token in [
        "loopeng",
        "agent.type: codex",
        "loopeng doctor",
        "--isolate",
        "a mock",        # require a REAL verifier — the "never a mock or `true`" prohibition
        "ledger",        # report the ledger
        "exit code",     # report the exit code
    ]:
        assert token in text, f"AGENTS.md should mention {token!r}"
    # pin the removed-flag fix so AGENTS.md prose can never re-introduce the bad advice
    assert "--ask-for-approval" not in text
    assert "approval_policy" in text
