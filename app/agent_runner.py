import json
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent_context import AgentTaskContext
from app.agent_result import (
    AgentError,
    AgentOutcome,
    AgentResult,
    EffectKind,
    ExecutionReceipt,
    ResultParseError,
    SideEffectState,
    parse_agent_result,
)
from app.config import env_int
from app.developer_prompt import prompt_template_variables
from app.history import safe_observability_error
from app.pi_tool_metadata import (
    reviewed_pi_command,
    structured_target_identifiers,
)
from app.process_runner import ProcessRunResult, run_process_with_idle_timeout
from app.pi_events import assistant_text_candidates, pi_session_id_from_payload
from app.pi_history import count_pi_session_lines, find_pi_session_path
from app.pi_runner import (
    PI_REPLY_AT_OPEN_DINGTALK_ID_ENV,
    PI_REPLY_SINGLE_CHAT_ENV,
    PI_TODO_TRIGGER_SENDER_NAME_ENV,
    PI_TODO_TRIGGER_SENDER_USER_ID_ENV,
    PI_TODO_TRIGGER_TEXT_ENV,
    PI_TODO_TRIGGER_CREATE_TIME_ENV,
    PiRunner,
    pi_process_failure_reason,
    selected_pi_routine_thinking_level,
    selected_pi_thinking_level,
)
from app.store import AgentRun, AgentRunLeaseLostError, AutoReplyStore, ReplyTask
from app.todo_routing import is_dingtalk_todo_create_intent, todo_create_tool_names


logger = logging.getLogger(__name__)


AGENT_RESULT_SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schemas" / "agent_result.schema.json"
)
SERVICE_ROOT = Path(__file__).resolve().parent.parent
SHARED_AGENT_RULES_PATH = Path.home() / ".agents" / "AGENT.md"
TOTAL_TIMEOUT_SECONDS = 1200
IDLE_TIMEOUT_SECONDS = 900
LEASE_SECONDS = TOTAL_TIMEOUT_SECONDS + IDLE_TIMEOUT_SECONDS + 300
DEFAULT_MAX_TOOL_CALLS = 30
DEFAULT_MAX_MEMORY_RECALL_CALLS = 2
DEFAULT_MAX_WORKSPACE_SEARCH_CALLS = 3
DEFAULT_MAX_WORKSPACE_READ_CALLS = 10
PI_FINALIZATION_FAILED_AFTER_CONFIRMED_EFFECT = (
    "pi_finalization_failed_after_confirmed_effect"
)
PI_TOOL_BUDGET_EXCEEDED = "pi_tool_budget_exceeded"

# Public, time-sensitive questions should use the public web bridge directly.
# They are deliberately kept out of the local-workspace retrieval path: the
# latter is for repository/project evidence and is both slower and irrelevant
# to questions such as "北京天气怎么样？".  This is a routing guard, not a
# replacement for the model's judgment; explicit local-material markers or
# supplied materials keep the normal full tool set.
PUBLIC_LIVE_QUERY_PATTERN = re.compile(
    r"(?:"
    r"天气|气温|温度|降雨|下雨|降雪|空气质量|湿度|风力|"
    r"新闻|热搜|头条|实时|最新消息|当前汇率|汇率|股价|股票|"
    r"路况|交通状况|航班状态|列车状态|比赛比分|赛事结果|"
    r"weather|forecast|news|latest|current\s+(?:price|rate|stock|weather)"
    r")",
    re.IGNORECASE,
)
PUBLIC_LOCAL_CONTEXT_PATTERN = re.compile(
    r"(?:"
    r"项目|仓库|代码|源码|服务|配置|部署|日志|测试|文档|附件|文件|"
    r"上面|刚才|本地|工作区|知识库|历史记录|审批|待办|日程|会议|"
    r"repository|workspace|local\s+file|project\s+file|attachment"
    r")",
    re.IGNORECASE,
)
PUBLIC_INFO_PI_TOOLS = (
    "web_search_exa",
    "web_fetch_exa",
    "execute_reviewed_write",
)
PUBLIC_INFO_READ_ONLY_PI_TOOLS = (
    "web_search_exa",
    "web_fetch_exa",
)
OA_PI_TOOLS = (
    "download_dingtalk_image",
    "execute_reviewed_read",
    "execute_reviewed_write",
)
OA_READ_ONLY_PI_TOOLS = (
    "download_dingtalk_image",
    "execute_reviewed_read",
)
DEFAULT_OA_APPROVAL_RULES_PATH = (
    SERVICE_ROOT / "app" / "defaults" / "oa_approval_rules.md"
)
MAX_OA_APPROVAL_RULES_BYTES = 64 * 1024
PI_JSON_FINALIZER_PROMPT = """The previous Direct Agent turn completed its work, but its final response was not valid AgentResult JSON.

Return exactly one valid AgentResult JSON object now. Do not call tools, do not perform any external action, and do not invent a new result. Reconstruct the final outcome only from the work and tool results already present in this same Pi session. The object must contain:

{"outcome":"completed|no_action|needs_human|failed","summary":"factual non-empty summary","error":{"code":"","retryable":false,"authorization_required":false}}

If the prior work is incomplete or its outcome cannot be verified, use needs_human or failed and explain the concrete reason in summary/error. Output JSON only, with no Markdown or surrounding text."""
DIRECT_AGENT_DEVELOPER_INSTRUCTIONS = """You are the Direct Agent for one queued task.

- The Agent owns evidence reads, business judgment, direct execution and verification.
- Use raw identifiers, references, exact read commands, and live tool results. Do not rely on service-side target assumptions.
- Complete authorized work only through the installed reviewed Pi tools. Use workspace_read/workspace_search/workspace_list for local evidence, graphify_read for the installed read-only Graphify query/explain/path operations, download_dingtalk_image for DingTalk robot image download codes, execute_reviewed_read/execute_reviewed_write for reviewed DWS operations, execute_reviewed_lark_read/execute_reviewed_lark_write for reviewed Lark operations, the explicitly registered Memory tools for Friday Memory, Exa for public web reads, and Xiaoqing tools for reviewed interview operations when configured. Arbitrary bash, edit, write, authentication, package installation, destructive commands, and unregistered MCP capabilities are unavailable. Do not produce plans, action arrays, or requests for service execution.
- DingTalk TODO intent has priority over Memory. Phrases such as “记一个待办”, “创建待办”, “TODO”, “截止日期”, “周五前完成”, or “帮我记一下任务” mean that the requested side effect is a DingTalk Todo. Read the installed dingtalk-todo skill when needed, then use execute_reviewed_write with the exact reviewed DWS command `dws todo task create` (including the resolved title, executor, due time, and priority). Do not call memory_write or document_upload for a Todo request. If the due time or executor cannot be resolved reliably, return needs_human and ask one focused clarification instead of writing Memory.
- Ordinary DingTalk reply tasks must not write Friday Memory or upload documents to it. The Direct Agent does not expose memory_write/document_upload for these tasks; use Memory read tools only when historical evidence is actually needed. A Memory write is never a fallback for a failed or ambiguous business-tool action.
- Retrieval must converge. For an open-ended status or history question, use one focused evidence path first, do not repeatedly search the same workspace, and stop with a factual partial answer when the available evidence is insufficient. The service enforces a per-run tool budget; never try to work around it by repeating equivalent searches.
- If the original trigger is a DingTalk calendar/schedule notification and does not explicitly ask to accept, decline, reschedule, check conflicts, or perform another calendar action, return no_action after the supplied calendar evidence. Do not search Memory or the workspace for a passive calendar notification.
- For a DingTalk group reply to the original trigger, use `dws chat message reply` with `--at-open-dingtalk-ids <sender_open_dingtalk_id>` on the reply. A native reference (`--ref-msg-id`/`--ref-sender`) alone does not render a visible @. Do not add this mention in a single chat, and use the exact ID from Original trigger rather than guessing from a name.
- Return only one JSON result with outcome, summary, and error. The outcome is completed, no_action, needs_human, or failed; summary is a nonempty factual description; error is always an object with code, retryable, and authorization_required, using an empty code and false flags when there is no error.
- Never run authentication login, reset, or logout commands. Authentication readiness belongs to the service gate.
- Never expose credentials, tokens, cookies, authorization codes, signed URLs, or local credential paths.
- Read an applicable installed SKILL.md through workspace_read before using a business capability. The reviewed Memory tools are user_get, memory_recall, memory_get, timeline_get, memory_write, and document_upload when configured. Never pass user_id, graph_id, or graph_ids; authenticated ACL owns scope. Exa is read-only. Xiaoqing exposes five reads plus upload_interview_result; put native Xiaoqing MCP fields inside its arguments object, use dry_run=true when only validating, and never claim a real upload without a completed result-record receipt. Lark uses official lark-cli risk metadata: read and ordinary write are available, high-risk-write and auth/config/update commands are always rejected.
- For a DingTalk image material with a media ID, run the supplied reviewed `dws chat message download-media` command. The adapter replaces `<local-path>` with an isolated temporary file, returns the image pixels directly to this turn, and deletes the file. For a robot `download_code`, call download_dingtalk_image with that exact code; the bridge resolves and fetches the image without exposing its signed URL. Judge the image only after the tool result includes an image attachment; never claim to have seen an image from an ID, code, filename, URL, or localPath text alone.
- When an OA action is performed, include oa_action_receipt with the exact process_instance_id, task_id, action, remark, and put the live read-back result in oa_action_receipt.result. Use null when no OA action was performed.
- After any confirmed OA action (approve, reject, return, or comment), identify the OA originator from the approval detail and notify that applicant through DingTalk before returning AgentResult. State the actual action and, when relevant, the next node or material needed. Use the real originator identifier; do not notify someone merely because they forwarded the request. Verify the send was accepted. If the originator cannot be resolved or notification fails, report that concrete exception in the final summary; do not invent delivery.
- After an OA review that does not approve, reject, or return the approval, notify that same applicant through DingTalk before returning AgentResult. Say that the approval remains pending, give the concrete missing material or other factual reason, and state the next action needed. Verify the send was accepted; do not silently rely on an OA comment or a group reminder as notice to the applicant.
- Use the original conversation context and live tool results to decide and execute the task. Report the actual outcome without inventing success."""
READ_ONLY_DEVELOPER_INSTRUCTION = (
    "This invocation is read-only. Use the permitted Pi read tools only. "
    "Do not perform any external write, send, approval, comment, "
    "reaction, edit, login, reset, logout, or other state-changing action."
)
_PI_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "workspace_read",
        "workspace_search",
        "workspace_list",
        "graphify_read",
        "download_dingtalk_image",
        "execute_reviewed_read",
        "execute_reviewed_lark_read",
        "user_get",
        "memory_recall",
        "memory_get",
        "timeline_get",
        "web_search_exa",
        "web_fetch_exa",
        "search_candidates",
        "get_dashboard_stats",
        "get_interview_context",
        "download_attachment",
        "list_candidate_interviews",
    }
)
_PI_EFFECTFUL_TOOL_NAMES = frozenset(
    {
        "create_dingtalk_todo",
        "execute_reviewed_write",
        "execute_reviewed_lark_write",
        "memory_write",
        "document_upload",
        "upload_interview_result",
    }
)
_PI_XIAOQING_READ_TOOLS = frozenset(
    {
        "search_candidates",
        "get_dashboard_stats",
        "get_interview_context",
        "download_attachment",
        "list_candidate_interviews",
    }
)


def direct_agent_developer_instructions() -> str:
    if not SHARED_AGENT_RULES_PATH.is_file():
        return DIRECT_AGENT_DEVELOPER_INSTRUCTIONS
    shared_rules = SHARED_AGENT_RULES_PATH.read_text(encoding="utf-8").strip()
    if not shared_rules:
        return DIRECT_AGENT_DEVELOPER_INSTRUCTIONS
    return (
        DIRECT_AGENT_DEVELOPER_INSTRUCTIONS
        + "\n\nCanonical shared agent rules already loaded into this invocation. "
        "Do not re-read agent rule files through shell or exec.\n\n"
        + shared_rules
    )


def is_public_live_info_context(context: AgentTaskContext) -> bool:
    """Return whether this task is a self-contained public-information query.

    The check intentionally uses only the trigger and service-supplied
    evidence.  A question with attached/materialized project evidence, a
    manual rerun, or prior side-effect receipts must retain the full tool set.
    """

    if context.materials or context.prior_receipts or context.manual_rerun:
        return False
    text = context.trigger_text.strip()
    if not text or not PUBLIC_LIVE_QUERY_PATTERN.search(text):
        return False
    return PUBLIC_LOCAL_CONTEXT_PATTERN.search(text) is None


def is_dingtalk_oa_context(context: AgentTaskContext) -> bool:
    return context.channel == "dingtalk" and any(
        material.kind == "dingtalk_oa" for material in context.materials
    )


def load_oa_approval_rules(workspace: Path) -> tuple[str, str]:
    """Load one configured OA rule file or the packaged safe fallback.

    OA routing must not ask the model to discover this file: a missing or
    renamed workspace rule was the reason Pi broadened into repeated searches.
    The service resolves and injects the rules before starting Pi instead.
    """

    configured_value = prompt_template_variables()["oa_approval_rules"].strip()
    configured_path = Path(os.path.expandvars(configured_value)).expanduser()
    if not configured_path.is_absolute():
        configured_path = workspace / configured_path
    candidates = (
        (configured_path, f"configured:{configured_value}"),
        (DEFAULT_OA_APPROVAL_RULES_PATH, "packaged:conservative-fallback"),
    )
    for path, source in candidates:
        try:
            if not path.is_file() or path.stat().st_size > MAX_OA_APPROVAL_RULES_BYTES:
                continue
            rules = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if rules:
            return rules, source
    raise RuntimeError("OA approval rules are unavailable")


class AgentRunUnavailableError(RuntimeError):
    pass


class AgentConversationLockedError(RuntimeError):
    """Another Direct Agent invocation owns this conversation's Pi session."""

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        super().__init__("pi_session_locked")


class AgentStreamError(RuntimeError):
    pass


class AgentToolBudgetExceeded(RuntimeError):
    """The model exceeded the bounded tool budget for one run."""

    def __init__(self, tool_name: str, limit: int) -> None:
        self.tool_name = tool_name
        self.limit = limit
        super().__init__(f"{PI_TOOL_BUDGET_EXCEEDED}:{tool_name}:{limit}")


class AgentRunUnknownError(RuntimeError):
    def __init__(self, code: str, run_id: int) -> None:
        self.code = code
        self.run_id = run_id
        super().__init__(code)


class AgentRunNoEffectEvidenceError(RuntimeError):
    pass


class AgentReadOnlyViolationError(RuntimeError):
    pass


class ReconciliationProof(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observed_state: Literal["effect_present", "effect_absent"]


class ReconciliationError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = ""
    retryable: bool = False
    authorization_required: bool = False


class ReconciliationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    outcome: AgentOutcome
    summary: str = Field(min_length=1)
    proof: ReconciliationProof | None = None
    error: ReconciliationError = Field(default_factory=ReconciliationError)


@dataclass(frozen=True)
class AgentReconciliationRunResult:
    run_id: int
    result: ReconciliationResult
    transcript_start_line: int
    transcript_end_line: int
    events: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class UnknownEffectReference:
    call_id: str
    transport: str
    operation: str
    operation_digest: str
    target_identifiers: dict[str, str]


@dataclass(frozen=True)
class DirectAgentRunResult:
    run_id: int
    result: AgentResult
    transcript_start_line: int
    transcript_end_line: int
    events: tuple[dict[str, object], ...]
    receipts: tuple[ExecutionReceipt, ...] = ()


ProcessExecutor = Callable[..., ProcessRunResult]


class DirectAgentRunner:
    def __init__(
        self,
        *,
        store: AutoReplyStore,
        workspace: Path,
        node_binary: str | None = None,
        executor: ProcessExecutor | None = None,
        owner: str | None = None,
        session_exists: Callable[[str], bool] | None = None,
        total_timeout_seconds: int | None = None,
        idle_timeout_seconds: int | None = None,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.pi = PiRunner(
            workspace=workspace,
            node_binary=node_binary,
        )
        self.executor = executor or run_process_with_idle_timeout
        self.owner = owner or f"direct-agent-{uuid4().hex}"
        self.total_timeout_seconds = (
            total_timeout_seconds
            if total_timeout_seconds is not None
            else env_int("CEO_PI_TIMEOUT_SECONDS", TOTAL_TIMEOUT_SECONDS)
        )
        self.idle_timeout_seconds = (
            idle_timeout_seconds
            if idle_timeout_seconds is not None
            else env_int("CEO_PI_IDLE_TIMEOUT_SECONDS", IDLE_TIMEOUT_SECONDS)
        )
        if self.total_timeout_seconds <= 0 or self.idle_timeout_seconds <= 0:
            raise ValueError("Pi timeouts must be positive")
        self.max_tool_calls = env_int(
            "CEO_PI_MAX_TOOL_CALLS", DEFAULT_MAX_TOOL_CALLS
        )
        self.max_memory_recall_calls = env_int(
            "CEO_PI_MAX_MEMORY_RECALL_CALLS", DEFAULT_MAX_MEMORY_RECALL_CALLS
        )
        self.max_workspace_search_calls = env_int(
            "CEO_PI_MAX_WORKSPACE_SEARCH_CALLS",
            DEFAULT_MAX_WORKSPACE_SEARCH_CALLS,
        )
        self.max_workspace_read_calls = env_int(
            "CEO_PI_MAX_WORKSPACE_READ_CALLS", DEFAULT_MAX_WORKSPACE_READ_CALLS
        )
        if any(
            value <= 0
            for value in (
                self.max_tool_calls,
                self.max_memory_recall_calls,
                self.max_workspace_search_calls,
                self.max_workspace_read_calls,
            )
        ):
            raise ValueError("Pi tool budgets must be positive")
        self.lease_seconds = (
            self.total_timeout_seconds + self.idle_timeout_seconds + 300
        )
        self.session_exists = session_exists or (
            lambda session_id: find_pi_session_path(session_id) is not None
        )

    def _build_agent_environment(
        self,
        context: AgentTaskContext,
        *,
        allow_group_reply_mention: bool,
    ) -> dict[str, str]:
        env = self.pi.build_env(preserve_local_cli_auth=True)
        if (
            allow_group_reply_mention
            and not context.single_chat
            and context.trigger_sender_open_dingtalk_id.strip()
        ):
            env[PI_REPLY_AT_OPEN_DINGTALK_ID_ENV] = (
                context.trigger_sender_open_dingtalk_id.strip()
            )
            env[PI_REPLY_SINGLE_CHAT_ENV] = "0"
        # The Todo capability resolves `trigger_sender` without asking the
        # model to guess a DingTalk user id.  These values are invocation-scoped
        # and are never inherited by the reviewed DWS child process.
        env[PI_TODO_TRIGGER_SENDER_NAME_ENV] = context.trigger_sender.strip()
        env[PI_TODO_TRIGGER_TEXT_ENV] = context.trigger_text.strip()
        env[PI_TODO_TRIGGER_CREATE_TIME_ENV] = context.trigger_create_time.strip()
        if context.trigger_sender_user_id.strip():
            env[PI_TODO_TRIGGER_SENDER_USER_ID_ENV] = (
                context.trigger_sender_user_id.strip()
            )
        else:
            env.pop(PI_TODO_TRIGGER_SENDER_USER_ID_ENV, None)
        return env

    @staticmethod
    def _thinking_level_for_context(
        context: AgentTaskContext,
        *,
        read_only: bool,
    ) -> str:
        """Choose the smallest safe Pi thinking pass for this task.

        A short trigger with no supplied history/materials is a routine reply:
        it should not pay the full reasoning cost on every turn.  Evidence
        backed, retried, or multi-message tasks keep the configured main level.
        Read-only diagnostics also retain the main level because they commonly
        require comparing several live results.
        """

        if (
            read_only
            or context.manual_rerun is not None
            or bool(context.materials)
            or bool(context.prior_receipts)
            or bool(context.messages)
            or len(context.trigger_text.strip()) > 240
        ):
            return selected_pi_thinking_level()
        return selected_pi_routine_thinking_level()

    def run(
        self,
        task: ReplyTask,
        context: AgentTaskContext,
        *,
        read_only: bool = False,
        now: str | None = None,
    ) -> DirectAgentRunResult:
        if context.task_id != task.id:
            raise ValueError("agent context task does not match reply task")
        lock_owner = f"direct-agent:{task.id}:{task.execution_generation}"
        if not self.store.acquire_agent_session_lock(
            task.conversation_id,
            lock_owner,
        ):
            raise AgentConversationLockedError(task.conversation_id)
        run_failed = False
        try:
            return self._run_with_session_lock(
                task,
                context,
                read_only=read_only,
                now=now,
            )
        except BaseException:
            run_failed = True
            raise
        finally:
            released = self.store.release_agent_session_lock(
                task.conversation_id,
                lock_owner,
            )
            if not released and not run_failed:
                raise RuntimeError("Pi session lock release failed")

    def _run_with_session_lock(
        self,
        task: ReplyTask,
        context: AgentTaskContext,
        *,
        read_only: bool = False,
        now: str | None = None,
    ) -> DirectAgentRunResult:
        claim = self.store.claim_agent_run(
            task.id,
            task.execution_generation,
            owner=self.owner,
            lease_seconds=self.lease_seconds,
            now=now,
        )
        if not claim.claimed:
            raise AgentRunUnavailableError(
                f"agent run is not available for task generation: {task.id}"
            )
        run = claim.run
        run_started_monotonic = time.monotonic()
        session_id = (
            run.agent_session_id
            or self.store.get_agent_session_id(task.conversation_id)
            or None
        )
        if session_id and not self.session_exists(session_id):
            self.store.clear_agent_session(task.conversation_id)
            session_id = None
        transcript_start_line = count_pi_session_lines(session_id) if session_id else 0
        prompt = context.render()
        developer_instructions = direct_agent_developer_instructions()
        approval_policy = "untrusted"
        if read_only:
            approval_policy = "never"
            prompt = (
                "Read-only invocation. Do not perform any external write, send, "
                "approval, comment, reaction, document edit, or state-changing "
                "command. Query live state only.\n\n" + prompt
            )
            developer_instructions += "\n\n" + READ_ONLY_DEVELOPER_INSTRUCTION
        thinking_level = self._thinking_level_for_context(
            context,
            read_only=read_only,
        )
        oa_context = is_dingtalk_oa_context(context)
        public_live_info = is_public_live_info_context(context)
        todo_create_intent = (
            context.channel == "dingtalk"
            and is_dingtalk_todo_create_intent(context.trigger_text)
        )
        tool_names = todo_create_tool_names(
            read_only=read_only,
            is_todo_intent=todo_create_intent,
        )
        if oa_context:
            # OA review has a deterministic evidence path. The approval rules
            # have one configured location and every business material read is
            # available through the supplied DWS commands, so broad workspace
            # search/list and unrelated connectors only add latency and create
            # a failure mode without adding evidence.
            tool_names = OA_READ_ONLY_PI_TOOLS if read_only else OA_PI_TOOLS
            approval_rules, approval_rules_source = load_oa_approval_rules(
                self.workspace
            )
            action_instruction = (
                "Do not perform an approval, comment, notification, or other "
                "write in this read-only run."
                if read_only
                else "Use execute_reviewed_write only for the authorized OA "
                "action/comment and the required applicant notification."
            )
            developer_instructions += (
                "\n\nThis is a DingTalk OA review. The OA workflow and safety "
                "rules are injected below; do not search for a skill or read, "
                "search, or list the workspace. Execute the exact supplied OA "
                "detail read command and only "
                "the DWS reads required by links or attachments in that live "
                "detail. Do not repeat an equivalent read. If those deterministic "
                "reads do not establish a safe decision, return needs_human with "
                "the concrete missing fact instead of broadening retrieval. "
                + action_instruction
                + "\n\nInjected OA approval rules (source: "
                + approval_rules_source
                + "):\n<oa_approval_rules>\n"
                + approval_rules
                + "\n</oa_approval_rules>"
            )
        elif todo_create_intent and not read_only:
            # A Todo request is deliberately a capability-scoped invocation.
            # The model may fill in natural-language fields, but it cannot
            # choose Memory, workspace search, or an unrelated DWS write.
            tool_names = ("create_dingtalk_todo",)
            developer_instructions += (
                "\n\nThis is an explicit DingTalk Todo creation request. "
                "The only available action is create_dingtalk_todo. Call it "
                "once with a concise title, executor_names as separate names "
                "(use `self` for the authenticated service user and "
                "`trigger_sender` for the original sender), an ISO-8601 due "
                "time when a deadline is stated, and priority 10/20/30/40. "
                "The tool resolves people, creates the Todo, and reads it back. "
                "If a required field is genuinely ambiguous, return "
                "needs_human; do not search the workspace or write Memory."
            )
        elif todo_create_intent:
            developer_instructions += (
                "\n\nThis is a DingTalk Todo request in read-only mode. "
                "Do not claim that a Todo was created; explain that live write "
                "execution is disabled in the current dry-run invocation."
            )
        elif public_live_info:
            tool_names = (
                PUBLIC_INFO_READ_ONLY_PI_TOOLS
                if read_only
                else PUBLIC_INFO_PI_TOOLS
            )
            delivery_instruction = (
                "Do not send or perform any other write in this read-only run."
                if read_only
                else "After answering, use the reviewed DingTalk write tool only "
                "to send the concise answer back to the original conversation."
            )
            developer_instructions += (
                "\n\nThis is a self-contained public, time-sensitive information "
                "question. Use web_search_exa/web_fetch_exa for current public "
                "evidence. Do not use workspace_read, workspace_search, "
                "workspace_list, Memory, Graphify, or DingTalk reads for this "
                "question. "
                + delivery_instruction
            )
        command = self.pi.build_command(
            prompt=prompt,
            session_id=session_id,
            output_schema_path=AGENT_RESULT_SCHEMA_PATH,
            use_output_schema=False,
            approval_policy=approval_policy,
            developer_instructions=developer_instructions,
            use_approval_bypass=not read_only,
            ignore_user_config=True,
            allow_memory_writes=False,
            thinking_level=thinking_level,
            tool_names=tool_names,
        )
        logger.info(
            "pi_agent_run_started task_id=%s run_id=%s thinking=%s read_only=%s "
            "session_reused=%s public_live_info=%s oa_context=%s",
            task.id,
            run.id,
            thinking_level,
            read_only,
            bool(session_id),
            public_live_info,
            oa_context,
        )
        saw_json = False
        stream_line_count = 0
        pi_tool_metadata: dict[str, dict[str, object]] = {}
        tool_call_count = 0
        memory_recall_count = 0
        workspace_search_count = 0
        workspace_read_count = 0

        def persist_line(line: str) -> None:
            nonlocal saw_json, stream_line_count
            nonlocal tool_call_count, memory_recall_count
            nonlocal workspace_search_count, workspace_read_count
            if not line.strip():
                return
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                if saw_json:
                    raise AgentStreamError("pi_stream_invalid") from exc
                return
            saw_json = True
            if not isinstance(payload, dict):
                raise AgentStreamError("pi_stream_invalid")
            if payload.get("type") == "tool_execution_start":
                tool_name = str(payload.get("toolName") or "tool").strip().casefold()
                tool_call_count += 1
                if tool_call_count > self.max_tool_calls:
                    raise AgentToolBudgetExceeded("all", self.max_tool_calls)
                if tool_name == "memory_recall":
                    memory_recall_count += 1
                    if memory_recall_count > self.max_memory_recall_calls:
                        raise AgentToolBudgetExceeded(
                            "memory_recall", self.max_memory_recall_calls
                        )
                elif tool_name == "workspace_search":
                    workspace_search_count += 1
                    if workspace_search_count > self.max_workspace_search_calls:
                        raise AgentToolBudgetExceeded(
                            "workspace_search", self.max_workspace_search_calls
                        )
                elif tool_name == "workspace_read":
                    workspace_read_count += 1
                    if workspace_read_count > self.max_workspace_read_calls:
                        raise AgentToolBudgetExceeded(
                            "workspace_read", self.max_workspace_read_calls
                        )
            stream_line_count += 1
            self.store.renew_agent_run_lease(
                run.id,
                owner=self.owner,
                lease_seconds=self.lease_seconds,
                now=now,
            )
            session_id = _session_id(payload)
            if session_id:
                self.store.set_agent_run_session(
                    run.id,
                    session_id,
                    owner=self.owner,
                    transcript_start_line=transcript_start_line,
                    now=now,
                )
                self.store.upsert_conversation(
                    task.conversation_id,
                    task.conversation_title,
                    task.single_chat,
                    session_id,
                )
            evidence = _pi_tool_evidence_event(
                payload,
                active_metadata=pi_tool_metadata,
            )
            if evidence is not None:
                self.store.append_agent_run_event(
                    run.id,
                    evidence,
                    owner=self.owner,
                    now=now,
                )
                _persist_pi_execution_receipt(
                    self.store,
                    run_id=run.id,
                    event=evidence,
                    owner=self.owner,
                    now=now,
                )

        def run_json_finalizer() -> AgentResult | None:
            """Repair only the final envelope, reusing the existing Pi session.

            A malformed final envelope is not evidence that the business work
            failed.  A no-tool, thinking-off turn lets Pi serialize the outcome
            it already reached without repeating any reviewed operation.
            """

            persisted_run = self.store.get_agent_run(run.id)
            finalizer_session_id = (
                persisted_run.agent_session_id if persisted_run else None
            ) or session_id
            if not finalizer_session_id:
                return None
            finalizer_prompt = PI_JSON_FINALIZER_PROMPT
            try:
                finalizer_command = self.pi.build_command(
                    prompt=finalizer_prompt,
                    session_id=finalizer_session_id,
                    approval_policy="never",
                    developer_instructions=(
                        "Return one strict AgentResult JSON object. "
                        "This is a serialization repair only.\n\n"
                        + finalizer_prompt
                    ),
                    use_approval_bypass=False,
                    allow_memory_writes=False,
                    thinking_level="off",
                    tool_names=(),
                )
                finalizer_process = self.executor(
                    finalizer_command,
                    prompt=finalizer_prompt,
                    env=self._build_agent_environment(
                        context,
                        allow_group_reply_mention=False,
                    ),
                    total_timeout_seconds=min(self.total_timeout_seconds, 120),
                    idle_timeout_seconds=min(self.idle_timeout_seconds, 60),
                    on_stdout_line=persist_line,
                )
            except (AgentStreamError, AgentRunLeaseLostError):
                return None
            except Exception:
                return None
            if finalizer_process.timed_out or finalizer_process.returncode != 0:
                return None
            if pi_process_failure_reason(
                finalizer_process.stdout,
                finalizer_process.stderr,
            ):
                return None
            try:
                return parse_agent_result(finalizer_process.stdout)
            except (ResultParseError, ValueError):
                return None

        try:
            process = self.executor(
                command,
                prompt=prompt,
                env=self._build_agent_environment(
                    context,
                    allow_group_reply_mention=not read_only,
                ),
                total_timeout_seconds=self.total_timeout_seconds,
                idle_timeout_seconds=self.idle_timeout_seconds,
                on_stdout_line=persist_line,
            )
        except AgentReadOnlyViolationError as exc:
            self._record_failure(run.id, str(exc), now=now)
            raise
        except AgentRunLeaseLostError:
            raise
        except AgentStreamError as exc:
            self._record_failure(
                run.id,
                "pi_stream_invalid",
                now=now,
            )
            if self.store.get_agent_run(run.id).status == "unknown":
                raise AgentRunUnknownError("pi_stream_invalid", run.id) from exc
            raise RuntimeError("pi_stream_invalid") from exc
        except AgentToolBudgetExceeded as exc:
            self._record_failure(
                run.id,
                PI_TOOL_BUDGET_EXCEEDED,
                detail=str(exc),
                retryable=False,
                now=now,
            )
            if self.store.get_agent_run(run.id).status == "unknown":
                raise AgentRunUnknownError(PI_TOOL_BUDGET_EXCEEDED, run.id) from exc
            raise RuntimeError(PI_TOOL_BUDGET_EXCEEDED) from exc
        except Exception as exc:
            self._record_failure(
                run.id,
                "pi_process_failed",
                detail=safe_observability_error(str(exc)),
                now=now,
            )
            if self.store.get_agent_run(run.id).status == "unknown":
                raise AgentRunUnknownError("pi_process_failed", run.id) from exc
            raise RuntimeError("pi_process_failed") from exc

        logger.info(
            "pi_agent_process_finished task_id=%s run_id=%s elapsed_seconds=%.3f "
            "tool_calls=%s stream_lines=%s returncode=%s",
            task.id,
            run.id,
            time.monotonic() - run_started_monotonic,
            tool_call_count,
            stream_line_count,
            process.returncode,
        )

        if process.timed_out:
            self._record_failure(
                run.id,
                "pi_process_timeout",
                now=now,
            )
            if self.store.get_agent_run(run.id).status == "unknown":
                raise AgentRunUnknownError("pi_process_timeout", run.id)
            raise RuntimeError("pi_process_timeout")
        if process.returncode != 0:
            self._record_failure(
                run.id,
                "pi_process_failed",
                detail=_process_failure_detail(process.stderr),
                now=now,
            )
            if self.store.get_agent_run(run.id).status == "unknown":
                raise AgentRunUnknownError("pi_process_failed", run.id)
            raise RuntimeError("pi_process_failed")
        pi_failure = pi_process_failure_reason(process.stdout, process.stderr)
        if pi_failure:
            code = pi_failure.partition(":")[0]
            self._record_failure(
                run.id,
                code,
                detail=pi_failure,
                now=now,
            )
            if self.store.get_agent_run(run.id).status == "unknown":
                raise AgentRunUnknownError(code, run.id)
            raise RuntimeError(code)
        persisted = self.store.get_agent_run(run.id)
        if persisted is None:
            raise RuntimeError("agent run was not persisted")
        try:
            result = parse_agent_result(process.stdout)
        except (ResultParseError, ValueError) as exc:
            result = run_json_finalizer()
            logger.info(
                "pi_agent_json_finalizer task_id=%s run_id=%s elapsed_seconds=%.3f "
                "succeeded=%s",
                task.id,
                run.id,
                time.monotonic() - run_started_monotonic,
                result is not None,
            )
            if result is None:
                self._record_failure(
                    run.id,
                    "pi_result_invalid",
                    now=now,
                )
                if self.store.get_agent_run(run.id).status == "unknown":
                    raise AgentRunUnknownError(
                        "pi_result_invalid", run.id
                    ) from exc
                raise RuntimeError("pi_result_invalid") from exc

        if todo_create_intent and result.outcome is AgentOutcome.COMPLETED:
            if not _has_confirmed_todo_creation(run_result_events := tuple(
                self.store.get_agent_run(run.id).tool_events
            )):
                result = AgentResult(
                    outcome=AgentOutcome.NEEDS_HUMAN,
                    summary=(
                        "待办请求尚未执行钉钉待办创建，也没有可核验的创建回执；"
                        "未把它写入长期记忆。"
                    ),
                    error=AgentError(
                        code="todo_creation_not_executed",
                        retryable=False,
                        authorization_required=False,
                    ),
                )

        persisted_session_id = self.store.get_agent_run(run.id).agent_session_id
        transcript_end_line = max(
            transcript_start_line + stream_line_count,
            count_pi_session_lines(persisted_session_id)
            if persisted_session_id
            else 0,
        )
        persisted = self.store.get_agent_run(run.id)
        if persisted is None:
            raise RuntimeError("agent run was not persisted")
        if persisted.side_effect_state not in {
            SideEffectState.NONE.value,
            SideEffectState.CONFIRMED.value,
        }:
            self.store.mark_agent_run_unknown(
                run.id,
                {"code": "pi_unreviewed_tool_effect", "retryable": False},
                owner=self.owner,
                transcript_end_line=transcript_end_line,
                now=now,
            )
            raise AgentRunUnknownError("pi_unreviewed_tool_effect", run.id)
        if (
            result.outcome is AgentOutcome.FAILED
            and persisted.side_effect_state == SideEffectState.CONFIRMED.value
        ):
            self.store.fail_agent_run(
                run.id,
                {
                    "code": PI_FINALIZATION_FAILED_AFTER_CONFIRMED_EFFECT,
                    "retryable": False,
                    "original_code": "pi_result_failed_after_effect",
                },
                owner=self.owner,
                side_effect_state=SideEffectState.CONFIRMED.value,
                transcript_end_line=transcript_end_line,
                now=now,
            )
            raise RuntimeError(PI_FINALIZATION_FAILED_AFTER_CONFIRMED_EFFECT)
        if result.outcome is AgentOutcome.FAILED:
            self.store.fail_agent_run(
                run.id,
                result.error.model_dump(mode="json"),
                owner=self.owner,
                side_effect_state=persisted.side_effect_state,
                transcript_end_line=transcript_end_line,
                now=now,
            )
        else:
            self.store.complete_agent_run(
                run.id,
                result.model_dump(mode="json"),
                owner=self.owner,
                side_effect_state=persisted.side_effect_state,
                transcript_end_line=transcript_end_line,
                now=now,
            )
        completed_run = self.store.get_agent_run(run.id)
        if completed_run is None:
            raise RuntimeError("agent run was not persisted")
        logger.info(
            "pi_agent_run_finished task_id=%s run_id=%s elapsed_seconds=%.3f "
            "outcome=%s tool_calls=%s transcript_lines=%s",
            task.id,
            run.id,
            time.monotonic() - run_started_monotonic,
            result.outcome.value,
            tool_call_count,
            max(0, completed_run.transcript_end_line - transcript_start_line),
        )
        return DirectAgentRunResult(
            run_id=run.id,
            result=result,
            transcript_start_line=transcript_start_line,
            transcript_end_line=completed_run.transcript_end_line,
            events=tuple(completed_run.tool_events),
            receipts=_execution_receipts_for_run(self.store, run.id),
        )

    def reconcile(
        self,
        existing_run: AgentRun,
        context: AgentTaskContext,
        *,
        now: str | None = None,
    ) -> AgentReconciliationRunResult:
        if existing_run.status != "unknown":
            raise ValueError("reconciliation requires an unknown agent run")
        if context.task_id != existing_run.reply_task_id:
            raise ValueError("agent context does not match unknown run")
        task = self.store.get_reply_task(existing_run.reply_task_id)
        if task is None:
            raise ValueError("reconciliation task does not exist")
        lock_owner = (
            f"direct-agent-reconcile:{task.id}:{existing_run.execution_generation}"
        )
        if not self.store.acquire_agent_session_lock(
            task.conversation_id,
            lock_owner,
        ):
            raise AgentConversationLockedError(task.conversation_id)
        run_failed = False
        try:
            return self._reconcile_with_session_lock(existing_run, context, now=now)
        except BaseException:
            run_failed = True
            raise
        finally:
            released = self.store.release_agent_session_lock(
                task.conversation_id,
                lock_owner,
            )
            if not released and not run_failed:
                raise RuntimeError("Pi reconciliation session lock release failed")

    def _reconcile_with_session_lock(
        self,
        existing_run: AgentRun,
        context: AgentTaskContext,
        *,
        now: str | None,
    ) -> AgentReconciliationRunResult:
        claim = self.store.claim_unknown_agent_run(
            existing_run.id,
            owner=self.owner,
            lease_seconds=self.lease_seconds,
            now=now,
        )
        if not claim.claimed:
            raise AgentRunUnavailableError(
                f"agent reconciliation is not available: {existing_run.id}"
            )
        run = claim.run
        original = unknown_effect_reference(run.tool_events)
        prompt = _reconciliation_prompt(context, original)
        developer_instructions = (
            direct_agent_developer_instructions()
            + "\n\n"
            + READ_ONLY_DEVELOPER_INSTRUCTION
            + " Return only one ReconciliationResult JSON object with outcome, "
            "summary, proof, and error. When live read evidence cannot prove presence "
            "or absence, return needs_human with a retryable error; never guess."
        )
        session_id = run.agent_session_id or None
        if session_id and not self.session_exists(session_id):
            session_id = None
        command = self.pi.build_command(
            prompt=prompt,
            session_id=session_id,
            use_output_schema=False,
            approval_policy="never",
            developer_instructions=developer_instructions,
            use_approval_bypass=False,
            ignore_user_config=True,
            allow_memory_writes=False,
        )
        active_metadata: dict[str, dict[str, object]] = {}
        events: list[dict[str, object]] = []
        saw_json = False

        def persist_line(line: str) -> None:
            nonlocal saw_json
            if not line.strip():
                return
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                if saw_json:
                    raise AgentStreamError("pi_stream_invalid") from exc
                return
            if not isinstance(payload, dict):
                if saw_json:
                    raise AgentStreamError("pi_stream_invalid")
                return
            saw_json = True
            evidence = _pi_reconciliation_evidence_event(
                payload,
                active_metadata=active_metadata,
            )
            if evidence is None:
                return
            self.store.append_unknown_agent_run_event(
                run.id,
                evidence,
                owner=self.owner,
                now=now,
            )
            events.append(evidence)

        try:
            process = self.executor(
                command,
                prompt=prompt,
                env=self._build_agent_environment(
                    context,
                    allow_group_reply_mention=False,
                ),
                total_timeout_seconds=self.total_timeout_seconds,
                idle_timeout_seconds=self.idle_timeout_seconds,
                on_stdout_line=persist_line,
            )
        except (AgentReadOnlyViolationError, AgentRunLeaseLostError):
            raise
        except AgentStreamError as exc:
            raise RuntimeError("pi_stream_invalid") from exc
        except Exception as exc:
            raise RuntimeError("pi_process_failed") from exc
        if process.timed_out:
            raise RuntimeError("pi_process_timeout")
        if process.returncode != 0:
            raise RuntimeError("pi_process_failed")
        pi_failure = pi_process_failure_reason(process.stdout, process.stderr)
        if pi_failure:
            raise RuntimeError(pi_failure.partition(":")[0])
        result = _parse_reconciliation_result(process.stdout)
        _validate_reconciliation_proof(result, original, events)
        persisted = self.store.get_agent_run(run.id)
        if persisted is None:
            raise RuntimeError("agent run was not persisted")
        return AgentReconciliationRunResult(
            run_id=run.id,
            result=result,
            transcript_start_line=run.transcript_end_line,
            transcript_end_line=persisted.transcript_end_line,
            events=tuple(events),
        )

    def _record_failure(
        self,
        run_id: int,
        code: str,
        *,
        detail: str = "",
        retryable: bool = True,
        now: str | None,
    ) -> None:
        persisted = self.store.get_agent_run(run_id)
        if persisted is None:
            raise RuntimeError("agent run was not persisted")
        if persisted.status != "running":
            return
        error: dict[str, object] = {"code": code, "retryable": retryable}
        if detail:
            error["detail"] = safe_observability_error(detail)
        if persisted.side_effect_state not in {
            SideEffectState.NONE.value,
            SideEffectState.CONFIRMED.value,
        }:
            self.store.mark_agent_run_unknown(
                run_id,
                error,
                owner=self.owner,
                transcript_end_line=persisted.transcript_end_line,
                now=now,
            )
        elif persisted.side_effect_state == SideEffectState.CONFIRMED.value:
            # Every effectful operation has a durable, safe-to-confirm receipt.
            # The agent may still terminate before emitting its final JSON, but
            # the external write must not be treated as an unknown effect or
            # retried as if nothing happened.
            error = {
                "code": PI_FINALIZATION_FAILED_AFTER_CONFIRMED_EFFECT,
                "retryable": False,
                "original_code": code,
            }
            if detail:
                error["detail"] = safe_observability_error(detail)
            self.store.fail_agent_run(
                run_id,
                error,
                owner=self.owner,
                transcript_end_line=persisted.transcript_end_line,
                side_effect_state=SideEffectState.CONFIRMED.value,
                now=now,
            )
        else:
            self.store.fail_agent_run(
                run_id,
                error,
                owner=self.owner,
                transcript_end_line=persisted.transcript_end_line,
                side_effect_state=SideEffectState.NONE.value,
                now=now,
            )

def unknown_effect_reference(
    events: list[dict[str, object]] | tuple[dict[str, object], ...],
) -> UnknownEffectReference:
    started: dict[str, dict[str, object]] = {}
    closed: set[str] = set()
    saw_effectful = False
    saw_unreviewed = False
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        effect = metadata.get("effect") if isinstance(metadata, dict) else None
        if effect == EffectKind.EFFECTFUL.value:
            saw_effectful = True
        elif effect == EffectKind.UNREVIEWED.value:
            saw_unreviewed = True
        call_id = item.get("call_id") or item.get("id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if event.get("type") == "item.started" and isinstance(metadata, dict):
            if effect == EffectKind.EFFECTFUL.value:
                started[call_id] = metadata
        elif event.get("type") in {"item.completed", "item.failed"}:
            closed.add(call_id)
    incomplete = [
        (call_id, data) for call_id, data in started.items() if call_id not in closed
    ]
    if not incomplete and not saw_effectful and not saw_unreviewed:
        raise AgentRunNoEffectEvidenceError(
            "unknown_run_has_no_incomplete_effect"
        )
    if saw_unreviewed:
        raise ValueError("unknown_run_contains_unreviewed_effect")
    if not incomplete:
        raise ValueError("unknown_run_effect_identity_missing")
    if len(incomplete) != 1:
        raise ValueError("unknown_run_effect_count_invalid")
    call_id, metadata = incomplete[0]
    digest = metadata.get("command_digest") or metadata.get("operation_digest")
    operation = metadata.get("operation")
    transport = metadata.get("native_cli") or metadata.get("mcp_server")
    targets = metadata.get("target_identifiers")
    if not all(
        isinstance(value, str) and value for value in (digest, operation, transport)
    ):
        raise ValueError("unknown_effect_identity_incomplete")
    target_identifiers = (
        {
            str(key): str(value)
            for key, value in targets.items()
            if isinstance(key, str) and isinstance(value, str) and value
        }
        if isinstance(targets, dict)
        else {}
    )
    return UnknownEffectReference(
        call_id=call_id,
        transport=str(transport),
        operation=str(operation),
        operation_digest=str(digest),
        target_identifiers=target_identifiers,
    )


def _reconciliation_prompt(
    context: AgentTaskContext,
    original: UnknownEffectReference,
) -> str:
    identity = {
        "call_id": original.call_id,
        "transport": original.transport,
        "operation": original.operation,
        "operation_digest": original.operation_digest,
        "target_identifiers": original.target_identifiers,
    }
    return (
        "Read-only unknown side-effect reconciliation. Never replay the original "
        "operation. Run one exact reviewed read through execute_reviewed_read for "
        "DWS or execute_reviewed_lark_read for Lark; "
        "direct shell execution and every write tool are disabled. Query live state "
        "with reviewed read-only tools. Return completed "
        "only when the effect is present, no_action only when its absence is confirmed, "
        "and set proof.observed_state to effect_present or effect_absent. The service "
        "binds the unique matching completed live read receipt; do not reproduce "
        "internal call IDs or digests.\n\n"
        "Original uncertain operation\n"
        + json.dumps(identity, ensure_ascii=False, indent=2)
        + "\n\n"
        + context.render()
    )


def _parse_reconciliation_result(raw: str) -> ReconciliationResult:
    payloads: list[dict[str, object]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    for payload in reversed(payloads):
        pi_candidates = assistant_text_candidates(payload)
        if pi_candidates:
            try:
                return ReconciliationResult.model_validate_json(pi_candidates[-1])
            except ValidationError as exc:
                raise RuntimeError("reconciliation_result_invalid") from exc
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        candidate = item.get("text") or item.get("message")
        if not isinstance(candidate, str):
            continue
        try:
            return ReconciliationResult.model_validate_json(candidate)
        except ValidationError as exc:
            raise RuntimeError("reconciliation_result_invalid") from exc
    raise RuntimeError("reconciliation_result_invalid")


def _validate_reconciliation_proof(
    result: ReconciliationResult,
    original: UnknownEffectReference,
    events: list[dict[str, object]],
) -> None:
    if result.outcome not in {AgentOutcome.COMPLETED, AgentOutcome.NO_ACTION}:
        return
    proof = result.proof
    expected_state = (
        "effect_present"
        if result.outcome is AgentOutcome.COMPLETED
        else "effect_absent"
    )
    if proof is None or proof.observed_state != expected_state:
        raise RuntimeError("reconciliation_proof_invalid")
    matches = [
        event
        for event in events
        if _is_matching_reconciliation_read_event(event, original)
    ]
    if not matches:
        raise RuntimeError("reconciliation_proof_invalid")
    if len(matches) != 1:
        raise RuntimeError("reconciliation_proof_ambiguous")


def _is_matching_reconciliation_read_event(
    event: dict[str, object], original: UnknownEffectReference
) -> bool:
    if event.get("type") != "item.completed":
        return False
    item = event.get("item")
    if not isinstance(item, dict):
        return False
    metadata = item.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("effect") != "read_only":
        return False
    pi_reviewed_cli = (
        item.get("type") == "command_execution"
        and metadata.get("reviewed_pi_read") is True
        and metadata.get("native_cli") in {"dws", "lark-cli"}
    )
    query_targets = metadata.get("target_identifiers")
    operation_digest = metadata.get("command_digest") or metadata.get(
        "operation_digest"
    )
    call_id = item.get("call_id") or item.get("id")
    if (
        not pi_reviewed_cli
        or not isinstance(call_id, str)
        or not call_id
        or not isinstance(operation_digest, str)
        or not operation_digest
        or not isinstance(metadata.get("result_digest"), str)
        or not metadata.get("result_digest")
        or not isinstance(query_targets, dict)
        or not original.target_identifiers
    ):
        return False
    return all(
        any(
            _target_key_matches(original_key, query_key)
            and query_value == original_value
            for query_key, query_value in query_targets.items()
        )
        for original_key, original_value in original.target_identifiers.items()
    )


def _target_key_matches(left: str, right: str) -> bool:
    left_parts = _target_key_parts(left)
    right_parts = _target_key_parts(right)
    if left_parts == right_parts:
        return True
    return {left_parts, right_parts} == {
        ("instance", "id"),
        ("process", "instance", "id"),
    }


def _target_key_parts(value: str) -> tuple[str, ...]:
    parts: list[str] = []
    current = ""
    for character in value:
        if not character.isalnum():
            if current:
                parts.append(current.casefold())
                current = ""
            continue
        if current and character.isupper() and not current[-1].isupper():
            parts.append(current.casefold())
            current = character
        else:
            current += character
    if current:
        parts.append(current.casefold())
    return tuple(parts)


def _session_id(payload: dict[str, object]) -> str:
    pi_session_id = pi_session_id_from_payload(payload)
    if pi_session_id:
        return pi_session_id
    if payload.get("type") not in {"thread.started", "thread_started"}:
        return ""
    for key in ("thread_id", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pi_tool_evidence_event(
    payload: dict[str, object],
    *,
    active_metadata: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    event_type = str(payload.get("type") or "")
    if event_type not in {"tool_execution_start", "tool_execution_end"}:
        return None
    call_id = str(payload.get("toolCallId") or "").strip()
    if not call_id:
        return None
    tool_name = str(payload.get("toolName") or "tool").strip().casefold()
    if event_type == "tool_execution_start":
        metadata = _pi_tool_effect_metadata(
            tool_name,
            payload.get("args"),
        )
        active_metadata[call_id] = metadata
        return {
            "type": "item.started",
            "item": {
                "id": call_id,
                "type": "command_execution",
                "metadata": metadata,
            },
        }

    metadata = dict(
        active_metadata.pop(call_id, None)
        or _unreviewed_pi_tool_metadata(tool_name, payload.get("result"))
    )
    is_error = payload.get("isError") is True
    if is_error and metadata.get("effect") == EffectKind.EFFECTFUL.value:
        metadata["effect"] = EffectKind.UNREVIEWED.value
        metadata["uncertain_after_error"] = True
    elif metadata.get("effect") == EffectKind.EFFECTFUL.value:
        if tool_name == "create_dingtalk_todo":
            confirmation, issue = _pi_todo_write_confirmation(
                payload.get("result"),
                metadata=metadata,
            )
        elif tool_name == "execute_reviewed_write":
            confirmation, issue = _pi_reviewed_write_confirmation(
                payload.get("result"),
                metadata=metadata,
            )
        elif tool_name in {"memory_write", "document_upload"}:
            confirmation, issue = _pi_memory_write_confirmation(
                payload.get("result"),
                metadata=metadata,
                tool_name=tool_name,
            )
        elif tool_name == "upload_interview_result":
            confirmation, issue = _pi_xiaoqing_write_confirmation(
                payload.get("result"),
                metadata=metadata,
            )
        elif tool_name == "execute_reviewed_lark_write":
            confirmation, issue = _pi_lark_write_confirmation(
                payload.get("result"),
                metadata=metadata,
            )
        else:
            confirmation, issue = None, "pi_write_tool_unreviewed"
        if confirmation is None:
            metadata["effect"] = EffectKind.UNREVIEWED.value
            metadata["uncertain_after_unverified_result"] = True
            metadata["confirmation_issue"] = issue
        else:
            # The dedicated Todo adapter resolves people and builds the final
            # DWS argv internally. Persist its authoritative digest from the
            # completed receipt instead of the model's natural-language args.
            if tool_name == "create_dingtalk_todo":
                metadata["command_digest"] = confirmation["operation_digest"]
                metadata["reviewed_execution_digest"] = confirmation[
                    "operation_digest"
                ]
                metadata["target_identifiers"] = confirmation["target_identifiers"]
            metadata["reviewed_confirmation"] = confirmation
    return {
        "type": "item.failed" if is_error else "item.completed",
        "item": {
            "id": call_id,
            "type": "command_execution",
            "status": "failed" if is_error else "completed",
            "exit_code": 1 if is_error else 0,
            "metadata": metadata,
        },
    }


def _pi_reconciliation_evidence_event(
    payload: dict[str, object],
    *,
    active_metadata: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    event_type = str(payload.get("type") or "")
    if event_type not in {"tool_execution_start", "tool_execution_end"}:
        return None
    call_id = str(payload.get("toolCallId") or "").strip()
    if not call_id:
        raise AgentReadOnlyViolationError("reconciliation_tool_call_id_missing")
    tool_name = str(payload.get("toolName") or "").strip().casefold()
    if tool_name in {"workspace_read", "workspace_search", "workspace_list"}:
        return None
    if tool_name not in {"execute_reviewed_read", "execute_reviewed_lark_read"}:
        raise AgentReadOnlyViolationError("reconciliation_write_forbidden")
    if event_type == "tool_execution_start":
        metadata = _pi_tool_effect_metadata(
            tool_name,
            payload.get("args"),
        )
        if metadata.get("effect") != EffectKind.READ_ONLY.value:
            raise AgentReadOnlyViolationError("reconciliation_command_unreviewed")
        metadata["reviewed_pi_read"] = True
        active_metadata[call_id] = metadata
        return {
            "type": "item.started",
            "item": {
                "id": call_id,
                "type": "command_execution",
                "metadata": metadata,
            },
        }

    metadata = active_metadata.pop(call_id, None)
    if not isinstance(metadata, dict):
        raise AgentReadOnlyViolationError("reconciliation_tool_start_missing")
    is_error = payload.get("isError") is True
    if is_error:
        return {
            "type": "item.failed",
            "item": {
                "id": call_id,
                "type": "command_execution",
                "status": "failed",
                "exit_code": 1,
                "metadata": metadata,
            },
        }
    confirmation, issue = _pi_reviewed_read_confirmation(
        payload.get("result"),
        metadata=metadata,
    )
    if confirmation is None:
        raise AgentReadOnlyViolationError(issue)
    metadata["result_digest"] = confirmation["result_digest"]
    return {
        "type": "item.completed",
        "item": {
            "id": call_id,
            "type": "command_execution",
            "status": "completed",
            "exit_code": 0,
            "metadata": metadata,
        },
    }


def _pi_tool_effect_metadata(
    tool_name: str,
    arguments: object,
) -> dict[str, object]:
    if tool_name == "create_dingtalk_todo":
        normalized = _pi_nested_tool_arguments(arguments)
        canonical = json.dumps(
            {"tool": tool_name, "arguments": normalized},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return {
            "effect": EffectKind.EFFECTFUL.value,
            "native_cli": "dws",
            "operation": "todo task create",
            "command_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "target_identifiers": {},
        }
    if tool_name == "graphify_read":
        operation = ""
        if isinstance(arguments, dict):
            operation = str(arguments.get("operation") or "").strip().casefold()
        canonical = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return {
            "effect": EffectKind.READ_ONLY.value,
            "native_cli": "graphify",
            "operation": f"graphify {operation}".strip(),
            "command_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "target_identifiers": {},
        }
    if tool_name == "download_dingtalk_image":
        canonical = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return {
            "effect": EffectKind.READ_ONLY.value,
            "native_cli": "dws",
            "operation": "robot message image download",
            "command_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "target_identifiers": {},
        }
    if tool_name in _PI_XIAOQING_READ_TOOLS | {"upload_interview_result"}:
        normalized_arguments = _pi_nested_tool_arguments(arguments)
        dry_run = (
            tool_name == "upload_interview_result"
            and isinstance(normalized_arguments, dict)
            and normalized_arguments.get("dry_run") is True
        )
        effect = (
            EffectKind.READ_ONLY
            if tool_name in _PI_XIAOQING_READ_TOOLS or dry_run
            else EffectKind.EFFECTFUL
        )
        canonical = json.dumps(
            {"tool": tool_name, "arguments": normalized_arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return {
            "effect": effect.value,
            "native_cli": "xiaoqing_interview",
            "operation": tool_name,
            "command_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "target_identifiers": structured_target_identifiers(
                normalized_arguments
            ),
        }
    if tool_name in {
        "user_get",
        "memory_recall",
        "memory_get",
        "timeline_get",
        "memory_write",
        "document_upload",
    }:
        effect = (
            EffectKind.EFFECTFUL
            if tool_name in {"memory_write", "document_upload"}
            else EffectKind.READ_ONLY
        )
        canonical = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return {
            "effect": effect.value,
            "native_cli": "memory_connector",
            "operation": tool_name,
            "command_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "target_identifiers": structured_target_identifiers(arguments),
        }
    if tool_name in {
        "execute_reviewed_read",
        "execute_reviewed_write",
        "execute_reviewed_lark_read",
        "execute_reviewed_lark_write",
    }:
        normalized_arguments = _pi_nested_tool_arguments(arguments)
        native_command = reviewed_pi_command(tool_name, normalized_arguments)
        if native_command is not None:
            return {
                "effect": native_command.effect.value,
                "native_cli": native_command.cli,
                "operation": native_command.operation,
                "command_digest": native_command.operation_digest,
                "target_identifiers": native_command.target_identifiers,
                "reviewed_execution_digest": native_command.operation_digest,
            }
        return _unreviewed_pi_tool_metadata(tool_name, normalized_arguments)

    effect = (
        EffectKind.READ_ONLY
        if tool_name in _PI_READ_ONLY_TOOL_NAMES
        else EffectKind.EFFECTFUL
        if tool_name in _PI_EFFECTFUL_TOOL_NAMES
        else EffectKind.UNREVIEWED
    )
    canonical = json.dumps(
        {"tool": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return {
        "effect": effect.value,
        "native_cli": "pi",
        "operation": f"pi_tool:{tool_name}",
        "command_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "target_identifiers": structured_target_identifiers(arguments),
    }


def _has_confirmed_todo_creation(events: tuple[dict[str, object], ...]) -> bool:
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        confirmation = metadata.get("reviewed_confirmation")
        if (
            metadata.get("operation") == "todo task create"
            and metadata.get("native_cli") == "dws"
            and isinstance(confirmation, dict)
            and confirmation.get("completed") is True
            and confirmation.get("safe_to_confirm") is True
        ):
            return True
    return False


def _pi_nested_tool_arguments(arguments: object) -> object:
    if isinstance(arguments, dict) and isinstance(
        arguments.get("arguments"), dict
    ):
        nested = arguments.get("arguments")
        assert isinstance(nested, dict)
        # Pi extensions may include bookkeeping keys beside the actual tool
        # arguments.  The reviewed wrapper's argv remains authoritative.
        if "argv" in nested or set(arguments) == {"arguments"}:
            return nested
    return arguments


def _unreviewed_pi_tool_metadata(
    tool_name: str,
    arguments: object,
) -> dict[str, object]:
    canonical = json.dumps(
        {"tool": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return {
        "effect": EffectKind.UNREVIEWED.value,
        "native_cli": "pi",
        "operation": f"pi_tool:{tool_name}",
        "command_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "target_identifiers": structured_target_identifiers(arguments),
    }


def _persist_pi_execution_receipt(
    store: AutoReplyStore,
    *,
    run_id: int,
    event: dict[str, object],
    owner: str,
    now: str | None,
) -> None:
    if event.get("type") != "item.completed":
        return
    item = event.get("item")
    if not isinstance(item, dict) or item.get("exit_code") != 0:
        return
    metadata = item.get("metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("effect") != EffectKind.EFFECTFUL.value
    ):
        return
    call_id = str(item.get("id") or "").strip()
    cli = str(metadata.get("native_cli") or "").strip()
    operation = str(metadata.get("operation") or "").strip()
    digest = str(metadata.get("command_digest") or "").strip()
    targets = metadata.get("target_identifiers")
    confirmation = metadata.get("reviewed_confirmation")
    if (
        not all((call_id, cli, operation, digest))
        or not isinstance(targets, dict)
        or not isinstance(confirmation, dict)
        or confirmation.get("completed") is not True
        or confirmation.get("safe_to_confirm") is not True
    ):
        return
    store.record_agent_execution_receipt(
        run_id,
        receipt_id=f"pi-tool:{run_id}:{call_id}",
        operation_id=call_id,
        cli=cli,
        command_path=operation,
        command_digest=digest,
        target_identifiers={
            str(key): value
            for key, value in targets.items()
            if isinstance(value, str)
        },
        exit_code=0,
        owner=owner,
        now=now,
    )


def _pi_reviewed_write_confirmation(
    result: object,
    *,
    metadata: dict[str, object],
) -> tuple[dict[str, object] | None, str]:
    details = result.get("details") if isinstance(result, dict) else None
    if not isinstance(details, dict):
        return None, "pi_write_details_missing"
    if details.get("protocolVersion") != 1:
        return None, "pi_write_receipt_protocol_invalid"
    if details.get("cli") != "dws" or details.get("effect") != "write":
        return None, "pi_write_receipt_effect_invalid"
    if details.get("operation") != metadata.get("operation"):
        return None, "pi_write_receipt_operation_mismatch"
    expected_execution_digest = metadata.get("reviewed_execution_digest")
    if (
        not isinstance(expected_execution_digest, str)
        or not expected_execution_digest
        or details.get("operationDigest") != expected_execution_digest
    ):
        return None, "pi_write_receipt_digest_mismatch"
    expected_targets = metadata.get("target_identifiers")
    targets = details.get("targetIdentifiers")
    if not isinstance(expected_targets, dict) or targets != expected_targets:
        return None, "pi_write_receipt_targets_mismatch"
    exit_code = details.get("exitCode")
    if isinstance(exit_code, bool) or exit_code != 0:
        return None, "pi_write_receipt_exit_invalid"
    if details.get("completed") is not True:
        return None, "pi_write_receipt_incomplete"
    if details.get("safeToConfirm") is not True:
        return None, "pi_write_receipt_not_confirmable"
    return (
        {
            "protocol_version": 1,
            "operation": details["operation"],
            "operation_digest": details["operationDigest"],
            "target_identifiers": targets,
            "completed": True,
            "safe_to_confirm": True,
        },
        "",
    )


def _pi_lark_write_confirmation(
    result: object,
    *,
    metadata: dict[str, object],
) -> tuple[dict[str, object] | None, str]:
    details = result.get("details") if isinstance(result, dict) else None
    if not isinstance(details, dict):
        return None, "pi_lark_receipt_missing"
    if details.get("protocolVersion") != 1:
        return None, "pi_lark_receipt_protocol_invalid"
    if details.get("cli") != "lark-cli" or details.get("effect") != "write":
        return None, "pi_lark_receipt_effect_invalid"
    if details.get("operation") != metadata.get("operation"):
        return None, "pi_lark_receipt_operation_mismatch"
    expected_execution_digest = metadata.get("reviewed_execution_digest")
    if (
        not isinstance(expected_execution_digest, str)
        or not expected_execution_digest
        or details.get("operationDigest") != expected_execution_digest
    ):
        return None, "pi_lark_receipt_digest_mismatch"
    expected_targets = metadata.get("target_identifiers")
    if not isinstance(expected_targets, dict) or details.get(
        "targetIdentifiers"
    ) != expected_targets:
        return None, "pi_lark_receipt_targets_mismatch"
    exit_code = details.get("exitCode")
    if isinstance(exit_code, bool) or exit_code != 0:
        return None, "pi_lark_receipt_exit_invalid"
    if details.get("completed") is not True or details.get("safeToConfirm") is not True:
        return None, "pi_lark_receipt_not_confirmable"
    receipt = details.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("processingStatus") != "completed":
        return None, "pi_lark_receipt_missing"
    result_identifiers = receipt.get("resultIdentifiers")
    if not isinstance(result_identifiers, dict) or not result_identifiers:
        return None, "pi_lark_receipt_identity_invalid"
    reviewed_identifiers = {
        str(key): value.strip()
        for key, value in result_identifiers.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }
    if not reviewed_identifiers:
        return None, "pi_lark_receipt_identity_invalid"
    return (
        {
            "protocol_version": 1,
            "operation": details["operation"],
            "operation_digest": details["operationDigest"],
            "target_identifiers": details["targetIdentifiers"],
            "completed": True,
            "safe_to_confirm": True,
            "receipt": {
                "processing_status": "completed",
                **reviewed_identifiers,
            },
        },
        "",
    )


def _pi_reviewed_read_confirmation(
    result: object,
    *,
    metadata: dict[str, object],
) -> tuple[dict[str, object] | None, str]:
    details = result.get("details") if isinstance(result, dict) else None
    if not isinstance(details, dict):
        return None, "reconciliation_query_receipt_invalid"
    if details.get("protocolVersion") != 1:
        return None, "reconciliation_query_receipt_invalid"
    expected_cli = metadata.get("native_cli")
    if (
        expected_cli not in {"dws", "lark-cli"}
        or details.get("cli") != expected_cli
        or details.get("effect") != "read"
    ):
        return None, "reconciliation_query_receipt_invalid"
    if details.get("operation") != metadata.get("operation"):
        return None, "reconciliation_query_receipt_invalid"
    expected_execution_digest = metadata.get("reviewed_execution_digest")
    if (
        not isinstance(expected_execution_digest, str)
        or not expected_execution_digest
        or details.get("operationDigest") != expected_execution_digest
    ):
        return None, "reconciliation_query_receipt_invalid"
    if details.get("targetIdentifiers") != metadata.get("target_identifiers"):
        return None, "reconciliation_query_receipt_invalid"
    exit_code = details.get("exitCode")
    if isinstance(exit_code, bool) or exit_code != 0:
        return None, "reconciliation_query_receipt_invalid"
    result_digest = details.get("resultDigest")
    if not isinstance(result_digest, str) or len(result_digest) != 64:
        return None, "reconciliation_query_receipt_invalid"
    if (
        details.get("completed") is not True
        or details.get("safeToConfirm") is not False
    ):
        return None, "reconciliation_query_receipt_invalid"
    return {"result_digest": result_digest}, ""


def _pi_todo_write_confirmation(
    result: object,
    *,
    metadata: dict[str, object],
) -> tuple[dict[str, object] | None, str]:
    """Validate the receipt emitted by the dedicated Todo adapter."""

    details = result.get("details") if isinstance(result, dict) else None
    if not isinstance(details, dict):
        return None, "pi_todo_receipt_missing"
    if (
        details.get("protocolVersion") != 1
        or details.get("cli") != "dws"
        or details.get("effect") != "write"
        or details.get("operation") != "todo task create"
    ):
        return None, "pi_todo_receipt_operation_invalid"
    if details.get("exitCode") != 0 or details.get("completed") is not True:
        return None, "pi_todo_receipt_incomplete"
    if details.get("safeToConfirm") is not True:
        return None, "pi_todo_receipt_not_confirmable"
    digest = details.get("operationDigest")
    if not isinstance(digest, str) or len(digest) != 64:
        return None, "pi_todo_receipt_digest_invalid"
    targets = details.get("targetIdentifiers")
    expected_targets = metadata.get("target_identifiers")
    if not isinstance(targets, dict) or not isinstance(expected_targets, dict):
        return None, "pi_todo_receipt_targets_invalid"
    if expected_targets and targets != expected_targets:
        return None, "pi_todo_receipt_targets_mismatch"
    if not isinstance(targets.get("taskId"), str) or not targets["taskId"].strip():
        return None, "pi_todo_receipt_task_id_missing"
    receipt = details.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("readbackVerified") is not True:
        return None, "pi_todo_receipt_readback_missing"
    task_id = receipt.get("taskId")
    if not isinstance(task_id, str) or not task_id.strip():
        return None, "pi_todo_receipt_task_id_missing"
    return (
        {
            "protocol_version": 1,
            "operation": "todo task create",
            "operation_digest": digest,
            "target_identifiers": targets,
            "completed": True,
            "safe_to_confirm": True,
            "receipt": {"task_id": task_id, "readback_verified": True},
        },
        "",
    )


def _pi_memory_write_confirmation(
    result: object,
    *,
    metadata: dict[str, object],
    tool_name: str,
) -> tuple[dict[str, object] | None, str]:
    details = result.get("details") if isinstance(result, dict) else None
    if not isinstance(details, dict):
        return None, "pi_memory_receipt_missing"
    if details.get("protocolVersion") != 1:
        return None, "pi_memory_receipt_protocol_invalid"
    if (
        details.get("bridge") != "memory_connector"
        or details.get("effect") != "write"
        or details.get("operation") != tool_name
        or details.get("operation") != metadata.get("operation")
    ):
        return None, "pi_memory_receipt_operation_invalid"
    if details.get("operationDigest") != metadata.get("command_digest"):
        return None, "pi_memory_receipt_digest_mismatch"
    if details.get("targetIdentifiers") != metadata.get("target_identifiers"):
        return None, "pi_memory_receipt_targets_mismatch"
    exit_code = details.get("exitCode")
    if isinstance(exit_code, bool) or exit_code != 0:
        return None, "pi_memory_receipt_exit_invalid"
    if details.get("completed") is not True or details.get("safeToConfirm") is not True:
        return None, "pi_memory_receipt_not_confirmable"
    receipt = details.get("receipt")
    if not isinstance(receipt, dict):
        return None, "pi_memory_receipt_missing"
    if tool_name == "memory_write":
        identifier = receipt.get("episode_uuid")
        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or receipt.get("processing_status") != "completed"
        ):
            return None, "pi_memory_receipt_identity_invalid"
    else:
        identifier = receipt.get("document_id")
        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or receipt.get("processing_status") != "completed"
        ):
            return None, "pi_memory_receipt_identity_invalid"
    return (
        {
            "protocol_version": 1,
            "operation": tool_name,
            "operation_digest": details["operationDigest"],
            "target_identifiers": details["targetIdentifiers"],
            "completed": True,
            "safe_to_confirm": True,
            "receipt": {
                str(key): value
                for key, value in receipt.items()
                if isinstance(value, str)
            },
        },
        "",
    )


def _pi_xiaoqing_write_confirmation(
    result: object,
    *,
    metadata: dict[str, object],
) -> tuple[dict[str, object] | None, str]:
    details = result.get("details") if isinstance(result, dict) else None
    if not isinstance(details, dict):
        return None, "pi_xiaoqing_receipt_missing"
    if details.get("protocolVersion") != 1:
        return None, "pi_xiaoqing_receipt_protocol_invalid"
    if (
        details.get("bridge") != "xiaoqing_interview"
        or details.get("effect") != "write"
        or details.get("operation") != "upload_interview_result"
        or details.get("operation") != metadata.get("operation")
    ):
        return None, "pi_xiaoqing_receipt_operation_invalid"
    if details.get("operationDigest") != metadata.get("command_digest"):
        return None, "pi_xiaoqing_receipt_digest_mismatch"
    if details.get("targetIdentifiers") != metadata.get("target_identifiers"):
        return None, "pi_xiaoqing_receipt_targets_mismatch"
    exit_code = details.get("exitCode")
    if isinstance(exit_code, bool) or exit_code != 0:
        return None, "pi_xiaoqing_receipt_exit_invalid"
    if details.get("completed") is not True or details.get("safeToConfirm") is not True:
        return None, "pi_xiaoqing_receipt_not_confirmable"
    receipt = details.get("receipt")
    if not isinstance(receipt, dict):
        return None, "pi_xiaoqing_receipt_missing"
    record_id = receipt.get("result_record_id")
    if (
        not isinstance(record_id, str)
        or not record_id.strip()
        or receipt.get("processing_status") != "completed"
    ):
        return None, "pi_xiaoqing_receipt_identity_invalid"
    return (
        {
            "protocol_version": 1,
            "operation": "upload_interview_result",
            "operation_digest": details["operationDigest"],
            "target_identifiers": details["targetIdentifiers"],
            "completed": True,
            "safe_to_confirm": True,
            "receipt": {
                "result_record_id": record_id.strip(),
                "processing_status": "completed",
            },
        },
        "",
    )


def _execution_receipts_for_run(
    store: AutoReplyStore,
    run_id: int,
) -> tuple[ExecutionReceipt, ...]:
    return tuple(
        ExecutionReceipt(
            receipt_id=receipt.receipt_id,
            operation_id=receipt.operation_id,
            completed=receipt.completed,
            persisted=receipt.persisted,
            safe_to_confirm=receipt.safe_to_confirm,
            cli=receipt.cli,
            operation=receipt.command_path,
            operation_digest=receipt.command_digest,
            target_identifiers=receipt.target_identifiers,
        )
        for receipt in store.list_agent_execution_receipts(run_id)
    )


def _process_failure_detail(stderr: str) -> str:
    for line in stderr.splitlines():
        candidate = line.strip()
        if candidate and " WARN " not in f" {candidate} ":
            return safe_observability_error(candidate, limit=500)
    return "Pi process exited without an error message"
