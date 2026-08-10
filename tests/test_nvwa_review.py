from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from app.nvwa_review import NvwaReviewError, review_work_profile_with_nvwa


class FakePiRunner:
    def __init__(self) -> None:
        self.commands: list[dict[str, object]] = []

    def build_command(self, prompt: str, session_id, **kwargs) -> list[str]:
        self.commands.append({"prompt": prompt, "session_id": session_id, **kwargs})
        return ["node", "pi.js"]

    def build_env(self) -> dict[str, str]:
        return {"SAFE": "1"}


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    skill = tmp_path / "skills" / "nvwa" / "SKILL.md"
    profile = tmp_path / "data" / "work-profile" / "work_profile.md"
    evidence = tmp_path / "data" / "profile-evidence" / "evidence_index.jsonl"
    style = tmp_path / "data" / "corpus" / "style_corpus.csv"
    for path in (skill, profile, evidence, style):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"seed {path.name}\n", encoding="utf-8")
    return skill, profile, evidence, style


def _completed_run(profile: Path, markdown: str):
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()

    def run(command, **kwargs):
        profile.write_text(markdown, encoding="utf-8")
        events = [
            {
                "type": "tool_execution_start",
                "toolCallId": "profile-write-1",
                "toolName": "write_work_profile",
                "args": {"markdown": markdown},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "profile-write-1",
                "toolName": "write_work_profile",
                "isError": False,
                "result": {
                    "details": {
                        "protocolVersion": 1,
                        "effect": "write",
                        "operation": "write_work_profile",
                        "operationDigest": digest,
                        "targetIdentifiers": {"artifact": "work_profile"},
                        "resultDigest": digest,
                        "exitCode": 0,
                        "completed": True,
                        "safeToConfirm": True,
                        "receipt": {
                            "artifact": "work_profile",
                            "sha256": digest,
                        },
                    }
                },
            },
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        ]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        )

    return run


def test_nvwa_review_uses_isolated_profile_tool_and_verifies_receipt(
    tmp_path: Path,
    monkeypatch,
):
    skill, profile, evidence, style = _inputs(tmp_path)
    monkeypatch.setattr("app.nvwa_review.NVWA_SKILL_CANDIDATES", (skill,))
    runner = FakePiRunner()
    markdown = "# Work profile\n\n- Verify outcomes before claiming completion.\n"

    result = review_work_profile_with_nvwa(
        workspace=tmp_path,
        profile_path=profile,
        evidence_index_path=evidence,
        style_corpus_path=style,
        runner=runner,
        run=_completed_run(profile, markdown),
    )

    assert result.profile_path == profile.resolve()
    assert result.sha256 == hashlib.sha256(markdown.encode()).hexdigest()
    assert result.bytes_written == len(markdown.encode())
    assert len(runner.commands) == 1
    invocation = runner.commands[0]
    assert invocation["approval_policy"] == "never"
    assert invocation["profile_distillation"] is True
    assert "write_work_profile exactly once" in invocation["developer_instructions"]
    assert str(skill.resolve()) in invocation["prompt"]


def test_nvwa_review_requires_installed_skill(tmp_path: Path, monkeypatch):
    _skill, profile, evidence, style = _inputs(tmp_path)
    monkeypatch.setattr("app.nvwa_review.NVWA_SKILL_CANDIDATES", ())

    with pytest.raises(NvwaReviewError, match="Nvwa skill is not installed"):
        review_work_profile_with_nvwa(
            workspace=tmp_path,
            profile_path=profile,
            evidence_index_path=evidence,
            style_corpus_path=style,
            runner=FakePiRunner(),
        )


def test_nvwa_review_rejects_local_path_leak(tmp_path: Path, monkeypatch):
    skill, profile, evidence, style = _inputs(tmp_path)
    monkeypatch.setattr("app.nvwa_review.NVWA_SKILL_CANDIDATES", (skill,))
    markdown = "# Work profile\n\nSource: /Users/private/raw.txt\n"

    with pytest.raises(NvwaReviewError, match="absolute local path"):
        review_work_profile_with_nvwa(
            workspace=tmp_path,
            profile_path=profile,
            evidence_index_path=evidence,
            style_corpus_path=style,
            runner=FakePiRunner(),
            run=_completed_run(profile, markdown),
        )


def test_nvwa_review_rejects_more_than_one_profile_write(tmp_path: Path, monkeypatch):
    skill, profile, evidence, style = _inputs(tmp_path)
    monkeypatch.setattr("app.nvwa_review.NVWA_SKILL_CANDIDATES", (skill,))
    markdown = "# Work profile\n\n- Keep writes singular.\n"
    valid_run = _completed_run(profile, markdown)

    def duplicate_run(command, **kwargs):
        completed = valid_run(command, **kwargs)
        events = [json.loads(line) for line in completed.stdout.splitlines()]
        duplicate_start = dict(events[0], toolCallId="profile-write-2")
        duplicate_end = dict(events[1], toolCallId="profile-write-2")
        events[2:2] = [duplicate_start, duplicate_end]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        )

    with pytest.raises(NvwaReviewError, match="confirmed profile receipt"):
        review_work_profile_with_nvwa(
            workspace=tmp_path,
            profile_path=profile,
            evidence_index_path=evidence,
            style_corpus_path=style,
            runner=FakePiRunner(),
            run=duplicate_run,
        )
