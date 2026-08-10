from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.pi_runner import PiRunner, pi_process_failure_reason


NVWA_SKILL_CANDIDATES = (
    Path.home() / ".agents" / "skills" / "nvwa" / "SKILL.md",
    Path.home() / ".agents" / "skills" / "nuwa" / "SKILL.md",
    Path.home() / ".agents" / "skills" / "huashu-nuwa" / "SKILL.md",
)
_ABSOLUTE_LOCAL_PATH = re.compile(r"(?:/Users/|/home/)[^\s`]+")


class NvwaReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class NvwaReviewResult:
    profile_path: Path
    sha256: str
    bytes_written: int
    skill_path: Path


def nvwa_skill_path() -> Path | None:
    return next((path for path in NVWA_SKILL_CANDIDATES if path.is_file()), None)


def review_work_profile_with_nvwa(
    *,
    workspace: Path,
    profile_path: Path,
    evidence_index_path: Path,
    style_corpus_path: Path,
    runner: PiRunner | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> NvwaReviewResult:
    skill = nvwa_skill_path()
    if skill is None:
        raise NvwaReviewError(
            "Nvwa skill is not installed at ~/.agents/skills/nvwa/SKILL.md"
        )
    required = (profile_path, evidence_index_path, style_corpus_path, skill)
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise NvwaReviewError(f"Nvwa review input is missing: {missing[0].name}")

    workspace = workspace.resolve()
    profile_path = profile_path.resolve()
    evidence_index_path = evidence_index_path.resolve()
    style_corpus_path = style_corpus_path.resolve()
    skill = skill.resolve()
    prompt = _nvwa_review_prompt(
        skill=skill,
        profile=profile_path,
        evidence=evidence_index_path,
        style=style_corpus_path,
    )
    pi = runner or PiRunner(workspace=workspace)
    command = pi.build_command(
        prompt,
        session_id=None,
        use_output_schema=False,
        approval_policy="never",
        developer_instructions=_NVWA_DEVELOPER_INSTRUCTIONS,
        profile_distillation=True,
    )
    env = pi.build_env()
    env["CEO_PI_WORK_PROFILE_PATH"] = str(profile_path)
    completed = run(
        command,
        input=prompt,
        text=True,
        capture_output=True,
        env=env,
        cwd=workspace,
        timeout=900,
        check=False,
    )
    if completed.returncode != 0:
        issue = pi_process_failure_reason(completed.stdout, completed.stderr)
        raise NvwaReviewError(issue or "Nvwa Pi review process failed")
    issue = pi_process_failure_reason(completed.stdout, completed.stderr)
    if issue:
        raise NvwaReviewError(issue)
    receipt = _confirmed_profile_receipt(completed.stdout)
    if receipt is None:
        raise NvwaReviewError("Nvwa review did not produce a confirmed profile receipt")
    content = profile_path.read_text(encoding="utf-8")
    if not content.strip():
        raise NvwaReviewError("Nvwa review produced an empty work profile")
    if _ABSOLUTE_LOCAL_PATH.search(content):
        raise NvwaReviewError("Nvwa review exposed an absolute local path")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if receipt.get("sha256") != digest:
        raise NvwaReviewError("Nvwa profile receipt digest does not match the file")
    return NvwaReviewResult(
        profile_path=profile_path,
        sha256=digest,
        bytes_written=len(content.encode("utf-8")),
        skill_path=skill,
    )


_NVWA_DEVELOPER_INSTRUCTIONS = """You are running one reviewed Nvwa work-profile distillation.

- Read the installed Nvwa SKILL.md first with workspace_read and follow it.
- Read only the supplied evidence index, style corpus, and current work profile.
- Distill stable work judgment, decision order, communication DNA, values, tensions, anti-patterns, and scenario-specific hard rules.
- Do not copy raw private excerpts, candidate/customer/personnel details, evidence IDs, tokens, session IDs, or absolute local paths into the profile.
- Preserve hard human-approval boundaries for HR, finance, legal, approvals, customer-critical commitments, and other real-world actions.
- Call write_work_profile exactly once with the complete final Markdown. It can update only the configured work_profile.md.
- Do not use DWS, Lark, Exa, Xiaoqing, Memory, shell, package installation, authentication, or any other write.
- After the confirmed write, return a short factual completion message."""


def _nvwa_review_prompt(
    *,
    skill: Path,
    profile: Path,
    evidence: Path,
    style: Path,
) -> str:
    return (
        "Perform the reviewed Nvwa work-profile distillation now.\n\n"
        f"Nvwa skill: {skill}\n"
        f"Current profile: {profile}\n"
        f"Evidence index: {evidence}\n"
        f"Style corpus: {style}\n\n"
        "Read all four files, then atomically replace only the configured work "
        "profile through write_work_profile."
    )


def _confirmed_profile_receipt(raw: str) -> dict[str, str] | None:
    starts: dict[str, dict[str, object]] = {}
    confirmed: list[dict[str, str]] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        call_id = str(event.get("toolCallId") or "").strip()
        if (
            event.get("type") == "tool_execution_start"
            and call_id
            and event.get("toolName") == "write_work_profile"
        ):
            starts[call_id] = event
            continue
        if (
            event.get("type") != "tool_execution_end"
            or not call_id
            or call_id not in starts
            or event.get("isError") is True
        ):
            continue
        result = event.get("result")
        details = result.get("details") if isinstance(result, dict) else None
        receipt = details.get("receipt") if isinstance(details, dict) else None
        if (
            not isinstance(details, dict)
            or details.get("protocolVersion") != 1
            or details.get("effect") != "write"
            or details.get("operation") != "write_work_profile"
            or details.get("targetIdentifiers") != {"artifact": "work_profile"}
            or details.get("exitCode") != 0
            or details.get("completed") is not True
            or details.get("safeToConfirm") is not True
            or not isinstance(receipt, dict)
            or receipt.get("artifact") != "work_profile"
            or not isinstance(receipt.get("sha256"), str)
            or receipt.get("sha256") != details.get("resultDigest")
        ):
            continue
        confirmed.append(
            {"artifact": "work_profile", "sha256": str(receipt["sha256"])}
        )
    return confirmed[0] if len(starts) == 1 and len(confirmed) == 1 else None
