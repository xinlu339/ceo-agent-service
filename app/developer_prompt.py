import importlib
import importlib.util
import os
import re
import sys
from pathlib import Path
from types import ModuleType

from app.config import (
    repo_root,
    user_alias,
    write_env_values,
)
from app.leak_check import FORBIDDEN_MARKERS


TAG_RE = re.compile(r"<(file|code|var):\s*([^>]+?)\s*>")
CODE_RE = re.compile(
    r"^([A-Za-z0-9_./-]+(?:\.[A-Za-z_][A-Za-z0-9_]*)?):"
    r"([A-Za-z_][A-Za-z0-9_]*)\(\)$"
)
VARIABLE_BLOCK_RE = re.compile(r"\A\s*<vars>\s*\n(?P<body>.*?)\n</vars>\s*", re.DOTALL)
VARIABLE_DEFINITION_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"
DEFAULT_DEVELOPER_PROMPT_TEMPLATE = repo_root() / "data" / "prompts" / "developer_prompt.md"
DEFAULT_USER_PROMPT_TEMPLATE = repo_root() / "data" / "prompts" / "user_prompt.md"
SEED_DEVELOPER_PROMPT_TEMPLATE = DEFAULTS_DIR / "developer_prompt.md"
SEED_USER_PROMPT_TEMPLATE = DEFAULTS_DIR / "user_prompt.md"
PROMPT_VARIABLE_ENV_PREFIX = "CEO_PROMPT_VAR_"
CONFIGURABLE_PROMPT_VARIABLE_DEFAULTS = {
    "responsibility_summary": (
        "Use the configured organization responsibility rules to decide whether "
        "the principal should reply."
    ),
    "forbidden_reply_text_terms": "",
    "oa_approval_rules": "management/OA/钉钉审批审阅原则.md",
    "calendar_rules_path": "management/OA/日历规则.md",
}

_LEGACY_PI_INTEGRATION_PROMPT_REPLACEMENTS = (
    (
        '- 当问题依赖本地知识图谱关系、跨文档背景或历史决策链时，可以使用 graphify。先阅读 `graphify-out/GRAPH_REPORT.md` 的相关部分，再用 `graphify query "<具体问题>"`、`graphify explain "<具体概念>"` 或 `graphify path "<A>" "<B>"` 找关系，并只打开与当前回复直接相关的文件。',
        "- 当问题依赖本地知识图谱关系、跨文档背景或历史决策链时，可以使用只读 graphify_read 工具。先阅读 `graphify-out/GRAPH_REPORT.md` 的相关部分，再按需要选择 query、explain 或 path 操作找关系，并只打开与当前回复直接相关的文件；如果工具报告 Graphify 未安装，不得改用 shell 或伪造图谱结果。",
    ),
    (
        "- 默认不了解当前业务背景；除非问题只是寒暄、确认收到、简单排期或上下文事实已经完整，否则先检索必要背景再判断。检索优先级是：当前消息和已注入上下文、本地文件、reviewed DWS 搜索与知识库工具，以及配置可用时的 Friday Memory reviewed read tools；同时善用 DWS 获取审批、日程、文档、链接、图片等材料。Lark、Xiaoqing 和 Exa 当前没有 reviewed Pi 工具，不得调用或声称调用。",
        "- 默认不了解当前业务背景；除非问题只是寒暄、确认收到、简单排期或上下文事实已经完整，否则先检索必要背景再判断。检索优先级是：当前消息和已注入上下文、本地文件、reviewed DWS 搜索与知识库工具、配置可用时的 Friday Memory、Exa 只读检索、Xiaoqing 招聘上下文，以及 Lark reviewed read tools；同时善用 DWS 获取审批、日程、文档、链接、图片等材料。只能调用本轮实际暴露的工具，未配置、未授权或未暴露的能力不得调用或声称调用。",
    ),
    (
        "- 当前运行不能写入长期 Memory。不要为了补偿这一缺口把一次性状态、系统运行事件、失败恢复过程或任务生命周期事件写入其他材料，也不要在 user_response.text 暴露 Memory、工具或运行时细节。",
        "\n".join(
            (
                "- 只有本轮实际暴露 memory_write 且产生后续会复用的业务信息时，才写入长期 Memory。可记录稳定业务事实、客户/项目背景、决策框架、审批/日历处理原则、客户沟通口径、长期偏好、已确认的组织关系或可复用判断结论。",
                "- 当 user_response.mode 是 send_reply，且回复包含可复用业务判断、客户口径、项目背景或稳定结论时，在输出最终 JSON 前调用 memory_write 记录一条业务 episode；episode 至少包含会话名、触发消息、mode、user_response.text、关键判断依据和可复用事实。",
                "- ask_clarifying_question 默认不写入长期 Memory；只有追问本身沉淀了稳定可复用的业务事实或判断规则时才写。单次补材料请求、临时澄清和未确认猜测不写入 Memory。",
                "- 日历/审批动作只有在形成可复用处理结论、规则或业务背景时才写 Memory；单次接受、拒绝、评论、退回等执行状态只进入审计。",
                "- 不要把一次性状态、系统运行事件、失败恢复过程或任务生命周期事件写入长期 Memory，例如 dry-run 恢复、send retry、launchd 重启、任务 pending/processing/failed 状态和工具报错。",
                "- memory_write 失败不应改变最终 JSON，也不要在 user_response.text 暴露工具或记忆写入细节。",
            )
        ),
    ),
    (
        "- Direct Agent 边界：DWS 可用性由服务在启动 Pi 前检查；你不得调用 dws auth login，也不得通过登录、刷新凭证或弹出授权页来修复依赖。你必须自行读取材料并只调用获准的 reviewed Pi 工具：本地只读工具、DWS reviewed read/write 工具，以及配置可用时的 Friday Memory tools。Lark、Xiaoqing、Exa、任意 bash 和未注册 MCP 均不可用。服务只负责校验、权限 gate、去重、事件与回执持久化以及投递。外部动作结果为 UNKNOWN 时必须停止自动重试并交由人工核对，不能假定成功或再次执行。",
        "- Direct Agent 边界：DWS/Lark 可用性由服务在启动 Pi 前检查；你不得调用 auth/login/logout/reset，也不得通过刷新凭证或弹出授权页来修复依赖。你必须自行读取材料并只调用获准的 reviewed Pi 工具；具体范围以本轮实际暴露的本地只读、DWS、Friday Memory、Exa、Xiaoqing 和 Lark adapter 为准。Exa 永远只读；Lark high-risk-write 永远阻断；Xiaoqing 上传、Memory 写入、Lark/DWS 普通写入只有在本轮确实暴露对应 write tool 时才可执行。任意 bash、通用文件写入、未注册 MCP 和未审查 CLI 均不可用。服务只负责校验、权限 gate、去重、事件与回执持久化以及投递。外部动作结果为 UNKNOWN 时必须停止自动重试并交由人工核对，不能假定成功或再次执行。",
    ),
)


class DeveloperPromptTemplateError(ValueError):
    pass


def developer_prompt_template_path() -> Path:
    return _configured_template_path(
        "CEO_DEVELOPER_PROMPT_TEMPLATE_PATH",
        DEFAULT_DEVELOPER_PROMPT_TEMPLATE,
    )


def user_prompt_template_path() -> Path:
    return _configured_template_path(
        "CEO_USER_PROMPT_TEMPLATE_PATH",
        DEFAULT_USER_PROMPT_TEMPLATE,
    )


def _configured_template_path(name: str, default: Path) -> Path:
    return Path(os.path.expandvars(os.getenv(name, str(default)))).expanduser()


def _ensure_template_file(template_path: Path, seed_path: Path) -> None:
    if template_path.exists():
        return
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(seed_path.read_text(encoding="utf-8"), encoding="utf-8")


def read_developer_prompt_template(path: Path | None = None) -> str:
    template_path = path or developer_prompt_template_path()
    if path is None:
        _ensure_template_file(template_path, SEED_DEVELOPER_PROMPT_TEMPLATE)
    template = template_path.read_text(encoding="utf-8")
    migrated = migrate_legacy_pi_integration_prompt(template)
    if migrated != template:
        template_path.write_text(migrated, encoding="utf-8")
    return migrated


def migrate_legacy_pi_integration_prompt(template: str) -> str:
    migrated = template
    for legacy, replacement in _LEGACY_PI_INTEGRATION_PROMPT_REPLACEMENTS:
        migrated = migrated.replace(legacy, replacement)
    return migrated


def read_user_prompt_template(path: Path | None = None) -> str:
    template_path = path or user_prompt_template_path()
    if path is None:
        _ensure_template_file(template_path, SEED_USER_PROMPT_TEMPLATE)
    return template_path.read_text(encoding="utf-8")


def write_developer_prompt_template(text: str, path: Path | None = None) -> Path:
    template_path = path or developer_prompt_template_path()
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(text, encoding="utf-8")
    return template_path


def write_user_prompt_template(text: str, path: Path | None = None) -> Path:
    template_path = path or user_prompt_template_path()
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(text, encoding="utf-8")
    return template_path


def render_developer_prompt(path: Path | None = None) -> str:
    return render_developer_prompt_template(read_developer_prompt_template(path))


def render_user_prompt(
    variables: dict[str, str],
    path: Path | None = None,
) -> str:
    return render_user_prompt_template(read_user_prompt_template(path), variables)


def render_developer_prompt_template(template: str) -> str:
    variable_definitions, body = split_developer_prompt_template(template)
    variables = prompt_template_variables()
    variables.update(parse_developer_prompt_variables(variable_definitions))

    return _render_template_tags(body, variables)


def render_user_prompt_template(
    template: str,
    runtime_variables: dict[str, str],
) -> str:
    from app.user_prompt_blocks import user_prompt_block_context

    variable_definitions, body = split_developer_prompt_template(template)
    variables = prompt_template_variables()
    variables.update(parse_developer_prompt_variables(variable_definitions))
    variables.update(runtime_variables)

    if not runtime_variables:
        return _render_template_tags(body, variables)
    with user_prompt_block_context(runtime_variables):
        return _render_template_tags(body, variables)


def split_developer_prompt_template(template: str) -> tuple[str, str]:
    match = VARIABLE_BLOCK_RE.match(template)
    if not match:
        return "", template
    return match.group("body").strip(), template[match.end() :].lstrip("\n")


def merge_developer_prompt_template(variable_definitions: str, body: str) -> str:
    variable_text = variable_definitions.strip()
    body_text = body.strip()
    if not variable_text:
        return body_text
    return f"<vars>\n{variable_text}\n</vars>\n\n{body_text}"


def developer_prompt_variable_pairs(variable_definitions: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(variable_definitions.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = VARIABLE_DEFINITION_RE.match(line)
        if not match:
            raise DeveloperPromptTemplateError(
                f"invalid variable definition on line {line_number}: {raw_line}"
            )
        pairs.append(match.groups())
    return pairs


def format_developer_prompt_variables(pairs: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    for key, value in pairs:
        name = key.strip()
        text = value.strip()
        if not name and not text:
            continue
        if not VARIABLE_DEFINITION_RE.match(f"{name} = {text}"):
            raise DeveloperPromptTemplateError(f"invalid variable name: {name}")
        lines.append(f"{name} = {text}")
    return "\n".join(lines)


def configurable_prompt_variable_pairs() -> list[tuple[str, str]]:
    variables = prompt_template_variables()
    return [
        (key, variables.get(key, ""))
        for key in CONFIGURABLE_PROMPT_VARIABLE_DEFAULTS
    ]


def prompt_variable_env_key(name: str) -> str:
    return f"{PROMPT_VARIABLE_ENV_PREFIX}{name.upper()}"


def write_configurable_prompt_variables(pairs: list[tuple[str, str]]) -> None:
    updates: dict[str, str] = {}
    allowed_keys = set(CONFIGURABLE_PROMPT_VARIABLE_DEFAULTS)
    env_key_to_template_key = {
        prompt_variable_env_key(key): key for key in allowed_keys
    }
    for key, value in pairs:
        name = key.strip()
        if not name and not value.strip():
            continue
        name = env_key_to_template_key.get(name, name)
        if name not in allowed_keys:
            raise DeveloperPromptTemplateError(
                f"unsupported config variable: {name}"
            )
        updates[prompt_variable_env_key(name)] = value.strip()
    write_env_values(updates)


def prompt_template_variables() -> dict[str, str]:
    variables = {
        "principal": user_alias(),
        "handoff_name": user_alias(),
    }
    for key, default in CONFIGURABLE_PROMPT_VARIABLE_DEFAULTS.items():
        variables[key] = os.getenv(prompt_variable_env_key(key), default)
    if not variables["forbidden_reply_text_terms"]:
        variables["forbidden_reply_text_terms"] = forbidden_reply_text_terms()
    return variables


def parse_developer_prompt_variables(variable_definitions: str) -> dict[str, str]:
    variables: dict[str, str] = {}
    for name, value in developer_prompt_variable_pairs(variable_definitions):
        variables[name] = _render_template_tags(value, {}, allow_variables=False)
    return variables


def _render_template_tags(
    template: str,
    variables: dict[str, str],
    *,
    allow_variables: bool = True,
) -> str:
    def replace(match: re.Match[str]) -> str:
        kind = match.group(1)
        expression = match.group(2).strip()
        if kind == "var":
            if not allow_variables:
                raise DeveloperPromptTemplateError(
                    "variable definitions cannot reference <var: ...> tags"
                )
            if expression not in variables:
                raise DeveloperPromptTemplateError(f"unknown template variable: {expression}")
            return variables[expression]
        if kind == "file":
            return _read_template_file(expression)
        if kind == "code":
            return _run_template_code(expression)
        raise DeveloperPromptTemplateError(f"unsupported template tag: {kind}")

    return TAG_RE.sub(replace, template)


def forbidden_reply_text_terms() -> str:
    return "、".join(f"`{marker}`" for marker in FORBIDDEN_MARKERS)


def _read_template_file(expression: str) -> str:
    path = _resolve_repo_path(expression)
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except OSError as exc:
        raise DeveloperPromptTemplateError(f"cannot read template file {path}: {exc}") from exc


def _run_template_code(expression: str) -> str:
    match = CODE_RE.match(expression)
    if not match:
        raise DeveloperPromptTemplateError(
            "code tag must look like <code: app.module:function()> "
            "or <code: scripts/file.py:function()>"
        )
    target, function_name = match.groups()
    module = _load_template_module(target)
    function = getattr(module, function_name, None)
    if not callable(function):
        raise DeveloperPromptTemplateError(f"template code is not callable: {expression}")
    result = function()
    return "" if result is None else str(result).rstrip()


def _load_template_module(target: str) -> ModuleType:
    if target.endswith(".py") or "/" in target:
        return _load_template_file_module(target)
    if not target.startswith("app."):
        raise DeveloperPromptTemplateError(
            "module code tags are restricted to app.* modules"
        )
    return importlib.import_module(target)


def _load_template_file_module(target: str) -> ModuleType:
    path = _resolve_repo_path(target)
    if path.suffix != ".py":
        raise DeveloperPromptTemplateError("file code tags must point to a .py file")
    module_name = f"_ceo_developer_prompt_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise DeveloperPromptTemplateError(f"cannot load template code file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise DeveloperPromptTemplateError(f"template code failed in {path}: {exc}") from exc
    return module


def _resolve_repo_path(value: str) -> Path:
    raw_path = Path(value)
    path = raw_path if raw_path.is_absolute() else repo_root() / raw_path
    resolved = path.resolve()
    root = repo_root().resolve()
    if not _is_relative_to(resolved, root):
        raise DeveloperPromptTemplateError(f"template path must stay inside repo: {value}")
    return resolved


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
