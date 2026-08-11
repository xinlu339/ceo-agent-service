# Codex → Pi Agent 功能等价矩阵

## 验收口径

本矩阵只比较 `ceo-agent-service` 在迁移基线 `d72eb90` 中实际使用的 Codex Runtime 能力，与当前 Pi Runtime 的实现。通过条件不是“代码里出现 Pi”或“Demo 能回复”，而是原有业务入口、工具能力、结构化输出、会话、审计、安全边界和失败行为在 Pi 下仍然成立。

旧 `codex_*` 数据库列、历史索引、兼容 CLI 参数和 `/codex` 只读重定向允许保留；它们不得再启动 Codex Runtime。

## 能力矩阵

| 能力 | Codex 基线行为 | Pi 实现 | 自动验证 | 本机真实状态 / 阻塞 |
|---|---|---|---|---|
| Runtime 启动 | Python Runner 启动 `codex exec` | 所有执行 Runner 统一启动平级 `../pi` 的 CLI；禁用 Pi builtin tools 和未审查 extensions，只加载仓库 reviewed extension | `tests/test_pi_runner.py`、Runtime 残留扫描 | Node 22.22.2 与 Pi CLI 可用；真实 Worker E2E 已确认启动 Pi 并完成工具调用 |
| Provider / Model | 使用 Codex 自身账号或 Provider 配置 | 支持 builtin/custom model source、Provider、Model、API protocol、Thinking、`CEO_PI_API_KEY`、`CEO_PI_BASE_URL`；API Key 只写入 mode-0600 `.env`，不进入命令行 | `tests/test_pi_runner.py`、`tests/test_pi_config.py`、`tests/test_pi_capabilities.py` | Provider doctor ready；已通过已配置 Provider 的真实 dry-run E2E |
| 会话续接 | 同会话复用 Codex session | 同会话复用 Pi session ID；旧 Codex session 不被 Pi resume | `tests/test_agent_runner.py`、`tests/test_structured_agent.py`、`tests/test_history.py` | 可用 |
| 旧历史 | Audit Web 可读 Codex JSONL | 旧 Codex 历史只读保留；新运行只写 Pi session；`/codex` 仅兼容重定向 | `tests/test_pi_config.py`、`tests/test_audit_web.py` | 可用 |
| Prompt / 用户自定义规则 | 运行时读取 `data/prompts/developer_prompt.md` | 保留用户自定义内容，只精确迁移已知的旧 Pi 限制句；真实 Prompt 已迁移 | `tests/test_prompt.py` | 可用 |
| 结构化输出 | Codex `--output-schema` + 本地模型校验 | Schema 注入 system prompt，Pi 返回后用 Pydantic 校验；必要时同 session 做一次只读修复 | `tests/test_agent_decision.py`、`tests/test_structured_agent.py`、各专用 Runner 测试 | 可用 |
| 本地文件读取 | Codex workspace/shell 读取 | `workspace_read/search/list`，限制 read roots、realpath/symlink、防越界、大小和输出上限 | `tests/test_pi_extension.py` | 可用 |
| Graphify | Prompt 允许 `graphify query/explain/path` | `graphify_read` 永久只读 adapter，只允许 query/explain/path，使用 `execFile` 而非 shell，隔离 Provider secrets | `tests/test_pi_extension.py`、`tests/test_pi_capabilities.py`、`tests/test_prompt.py` | `@sentropic/graphify` 0.17.1 已安装；doctor ready；真实 Pi `query`、`explain`、`path` 已通过 |
| DWS 只读 | Codex 可执行项目提供的 DWS read 命令 | `execute_reviewed_read` 只接受安装版 schema 标为 read 的准确命令；认证/安装/交互确认命令阻断 | `tests/test_pi_extension.py`、`tests/test_agent_runner.py` | Schema 473 read，ready |
| DWS 普通写入 | Codex 在授权路径执行非破坏性写入 | `execute_reviewed_write` 只接受 schema 标为 write、非 destructive、非 auth、非 user-confirmation 的命令；真实结果生成 receipt | `tests/test_pi_extension.py`、`tests/test_agent_runner.py` | Schema 435 write，ready；真实发送仍保持关闭 |
| 多能力同时开启 | Codex 可同时看到多个 MCP/CLI | Pi 同一轮可同时暴露 DWS、Lark、Memory、Exa、Xiaoqing、Graphify 和图片工具；dry-run 自动移除所有 write tools | `tests/test_pi_runner.py`、`tests/test_pi_extension.py` | 代码支持；已在同一真实 Pi 会话同时完成 Memory 与 Exa 只读调用；其他能力按本地授权独立 fail-closed |
| Friday Memory 读取 | `user_get/memory_recall/memory_get/timeline_get` | Reviewed Python bridge 使用官方 MCP client；ACL 由 API Key 身份决定，拒绝 `user_id/graph_id/graph_ids` | `tests/test_pi_memory_bridge.py`、`tests/test_pi_memory_e2e.py`、专用 Runner 测试 | Doctor ready；真实 `user_get({})` 已通过，未传任何 scope ID，也未输出个人内容 |
| Friday Memory 写入 | 业务稳定事实可 `memory_write/document_upload` | 仅非 dry-run 且工具确实暴露时可写；Prompt 恢复稳定事实和业务 episode 写入规则；写失败不改变最终回复 | `tests/test_prompt.py`、`tests/test_pi_extension.py`、WeChat Memory tests | 代码支持；本轮不做真实写入 |
| Exa | Codex passthrough MCP 可搜索公开资料 | `web_search_exa/web_fetch_exa` reviewed bridge，永久只读，拒绝私网、localhost、metadata 和内嵌凭证 URL | `tests/test_pi_exa_bridge.py`、`tests/test_pi_exa_e2e.py` | Doctor ready；真实 `web_search_exa` 公开搜索已通过 |
| Xiaoqing 读取 | 候选人判断可读取 Xiaoqing | 5 个 reviewed reads；Task Agent、OKR、Weekly Prompt 均恢复按需使用和终态关闭逻辑 | `tests/test_pi_xiaoqing_bridge.py`、`tests/test_task_agent.py`、OKR tests | Bridge 已安装；本机缺 OAuth token，`missing_auth` |
| Xiaoqing 写入 | 授权流程可上传面试结果 | `upload_interview_result`；dry-run 视为 read，真实写必须返回 completed result-record receipt，否则 unknown | `tests/test_pi_xiaoqing_bridge.py`、`tests/test_pi_extension.py` | 代码支持；缺本地 OAuth，未做真实写入 |
| Lark 读取/普通写入 | Codex passthrough MCP/CLI | 官方 `lark-cli schema` 和 shortcut help 提供风险分类；read 与普通 write 可同时启用；Node-backed CLI 继承配置的 Node 22 runtime | `tests/test_pi_extension.py`、`tests/test_agent_runner.py`、`tests/test_pi_capabilities.py`、`tests/test_channel_gate.py` | `lark-cli` 1.0.85 已安装；schema 96 read / 112 write ready；Channel Gate 因账号尚未配置而 blocked |
| Lark 高风险动作 | 受外部工具权限约束 | `high-risk-write`、danger、auth/config/update/install、dry-run 伪回执永久阻断 | `tests/test_pi_extension.py` | 安全边界不低于基线 |
| DingTalk 图片（mediaId） | Codex 可接收下载后的本地图片 | Reviewed DWS adapter 将输出重写到隔离临时文件，校验 PNG/JPEG/GIF/WebP 和 10 MiB 上限，把像素作为 Pi image content 返回并立即删除文件 | `tests/test_pi_extension.py`、`tests/test_worker.py` | 代码支持；最终 E2E 使用 fake DWS 验证，不下载真实消息 |
| DingTalk 图片（downloadCode） | Codex 可通过 DWS/外部读取机器人图片 | `download_dingtalk_image` bridge 用本地 DWS 身份解析一次性 URL，校验 HTTPS/私网/格式/大小，只返回像素，不返回签名 URL | `tests/test_pi_dingtalk_image_bridge.py`、`tests/test_pi_extension.py`、`tests/test_worker.py` | 代码支持；不消费真实消息 |
| NvWa | 只用于离线 work-profile 复核 | 专用 Runner 只暴露 workspace reads + `write_work_profile`，只能原子替换固定画像文件 | `tests/test_nvwa_review.py` | `huashu-nuwa` Skill 已安装并 doctor ready；真实 Pi profile review、原子写入和 receipt digest 校验已通过 |
| Meeting Alignment | 可读 work profile 与 Memory 历史案例；历史来源须有真实工具证据 | 恢复 `memory_recall`，并恢复历史来源防伪校验 | `tests/test_meeting_alignment_agent.py` | 代码支持；Memory ready |
| Task Agent | 结合项目/TODO/Memory/Xiaoqing 做状态更新 | 恢复候选人终态的 Xiaoqing 查询、TODO/follow-up 关闭与失败降级边界 | `tests/test_task_agent.py` | 代码支持；真实 Xiaoqing 受 OAuth 阻塞 |
| OKR Review / Weekly OKR | 可使用 DWS、Memory、Lark、Exa、Xiaoqing 读取证据 | 专用 Pi Runner 暴露同等 read tools；Prompt 不再错误宣称这些能力不支持 | `tests/test_okr_review.py`、`tests/test_weekly_okr_report.py` | 代码支持；外部真实状态见各 adapter 行 |
| WeChat 决策 | 只基于同会话内容，不调用外部工具 | 继续使用 tool-free Pi 决策；未扩大权限 | `tests/wechat/test_consumer.py`、`tests/wechat/test_decision_runner.py` | 与原安全设计一致 |
| WeChat Memory 去重/写入 | Matcher 只用 recall；Writer 只用 write | 分别强制单一 `memory_recall` / `memory_write`，并校验精确参数和 receipt | `tests/wechat/test_memory.py`、相关 Pi safety tests | 代码支持；不做真实写入 |
| Receipt / Trace / Unknown | Codex 工具事件用于审计和未知副作用判断 | Pi tool start/end 实时落库；write 必须匹配 operation digest、targets、result digest、receipt；不确定结果不重放 | `tests/test_agent_runner.py`、`tests/test_pi_tool_metadata.py` | 可用 |
| Idempotency / Reconciliation | generation、session lock、sent-reply 去重 | 保留 generation-aware claim、session lock、sent receipt；unknown 仅用 reviewed read reconciliation | `tests/test_agent_runner.py`、`tests/test_worker.py`、Store tests | 可用 |
| Audit Web / Feedback | 查看 attempt、session、trace、feedback | 新运行展示 Pi session；旧 Codex 只读；原有 UI 和入口不重做 | `tests/test_audit_web.py`、feedback tests | 8766 隔离验收服务由 launchd 常驻；原有手动 8765 进程未停止 |
| Dry-run | 不真实发送 | `CEO_DRY_RUN=1` 时 Direct Agent 只暴露 read tools，投递不发送，Memory/Xiaoqing/Lark/DWS write 均不可用 | `tests/test_pi_runner.py`、`tests/test_worker.py` | 当前本机保持开启 |

## 当前必须区分的三类状态

1. **实现完成且可自动验证**：代码、Prompt、工具注册、审计与测试已经具备。
2. **实现完成但本机缺外部账号授权**：Xiaoqing OAuth、Lark 登录。它们不能从支持范围删除，但也不能伪报真实调用已通过。Graphify、NvWa 和 Lark CLI 本体已经安装。
3. **明确不在本轮自动执行的真实副作用**：真实发钉钉/Lark/邮件、审批动作、Memory 写入、Xiaoqing 上传。它们保留代码能力和 fake/contract E2E，但在用户未单独授权目标前不触发。

## 2026-08-11 最终真实验证证据

- Pi Runtime 必需项均为 ready：Node.js `22.22.2`、平级 Pi CLI、reviewed extension、Provider/Model/API/Base URL 与 API Key 配置。
- 使用独立临时 workspace、独立 Pi agent/session 目录和独立 SQLite 构造一条 DingTalk reply task；Worker 完成 `pending → processing → done`，AgentRun 为 `completed`，AgentResult 为 `completed`，`side_effect_state=none`。
- 上述 Worker E2E 真实调用 `workspace_read`；`agent_run_events` 落库一条 `item.started` 和一条 `item.completed`，operation 为 `pi_tool:workspace_read`，effect 为 `read_only`。
- 隔离 E2E 中 `sent_replies=0`，没有 execution receipt，也没有触碰生产 pending task。
- 同一真实 Pi 只读会话同时调用 `user_get({})` 与 `web_search_exa({query: "Pi Agent official repository", numResults: 3})`；两个 reviewed bridge 均返回 `effect=read`、`completed=true`，Pi stream settled 且无 incomplete tool call。
- Runtime 启动残留扫描未发现 `CodexRunner`、`app.codex_runner`、`codex_bin`、`codex exec` 启动命令或以 `codex` 为子进程的执行路径。保留命中的旧错误文本、数据库列、CLI alias 和历史读取代码仅用于兼容旧数据。
- 隔离验收服务运行在 `127.0.0.1:8766`，使用独立 `pi-acceptance.sqlite3` 和 `CEO_SERVICE_MODE=dry-run`；Workers 显示 launchd running，pending / processing / retryable / failed / attention 均为 0。
- launchd 精简环境下的 Node-backed CLI 已复验：Lark schema 从错误的 `env: node: No such file or directory` 恢复为 ready；Graphify 与 Pi extension 同样继承配置的 Node runtime。

## 最终验收命令

```bash
.venv/bin/pytest -q
.venv/bin/python -m app.cli doctor-mcp --db /tmp/ceo-agent-pi-parity-doctor.sqlite3
.venv/bin/python -m app.cli channel-doctor
acceptance_workspace=$(mktemp -d /tmp/ceo-pi-provider-smoke.XXXXXX)
CEO_LIVE_PI_E2E=1 CEO_PI_E2E_WORKSPACE="$acceptance_workspace" \
  .venv/bin/pytest --run-live -q \
  tests/e2e/test_live_smoke.py::test_live_pi_exec_json_smoke
```

隔离 SQLite 的真实 Pi Worker dry-run、Friday Memory 只读探针和 Exa 只读探针均已完成。由于当前没有为真实副作用提供单独授权目标，最终验收仍不应发送消息、执行审批、写 Memory 或上传 Xiaoqing 面试结果。
