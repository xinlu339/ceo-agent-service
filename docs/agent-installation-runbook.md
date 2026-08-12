# Agent Installation Runbook

This runbook is for an agent installing CEO Agent Service on a user's Mac. The
agent should run commands, inspect outputs, edit local config, and report
blocking prompts. Do not ask the user to copy commands into Terminal. Ask the
user only for choices, credentials, QR-code confirmation, OS permission clicks,
or policy decisions that the agent cannot make.

## Install Contract

Goal: leave the machine with a verified local service, prepared corpus/profile
data, and an audit web UI that can be used to review behavior before live send.

Default safety:

- Start in dry-run mode.
- Do not send DingTalk messages until `CEO_NOT_SEND_MESSAGE=0` and
  `CEO_LIVE_SEND_BLOCKERS_ACCEPTED=1` are both explicitly confirmed.
- Do not commit or upload local chat exports, corpus files, SQLite databases,
  Pi sessions, DingTalk tokens, cookies, robot codes, or generated private
  evidence.
- Keep real work data outside the repository, normally under
  `~/Documents/memory`.
- Use the real user `HOME`; do not point `HOME` at the repository because
  `dws`, Pi, launchd, and local credentials depend on the user's normal
  profile directories.

## Phase 0: Collect Interactive Parameters

Collect these values before changing the machine. If a value is unknown, inspect
the local machine first and ask only when inspection cannot answer it.

| Parameter | Default | Notes |
| --- | --- | --- |
| Repository path | `~/Documents/Projects/ceo-agent-service` | Must be the service checkout. |
| Workspace path | `~/Documents/memory` | Local knowledge corpus, AI minutes, SOPs, and source docs. |
| Database path | `~/Library/Application Support/ceo-agent-service/auto-reply.sqlite3` | Local SQLite runtime state, kept outside iCloud-managed Documents. |
| Corpus path | `./data/corpus` | Ignored by Git; contains style corpus. |
| Principal display name | user supplied | Used in prompts, aliases, and handoff text. |
| Mention aliases | user supplied | Include exact DingTalk @ aliases, comma-separated. |
| Assistant signature | user supplied | Text appended to automated replies. |
| Handoff acknowledgement | user supplied | Text used when the agent should hand off. |
| Feedback web URL | optional | Vercel/base URL for thumbs-up/down links. |
| Pi Provider / Model / API protocol | `deepseek` / `deepseek-v4-pro` / `openai-completions` | Configure on `/config?tab=agent`; OpenAI and other providers remain available. |
| Pi Base URL | provider default | Optional custom provider endpoint. |
| Pi API Key | user supplied | Stored only in ignored `.env`; never echo it or place it in command arguments. |
| DingTalk KB workspace | optional | Workspace id or URL for profile evidence collection. |
| Live send opt-in | no by default | Ask only after dry-run evidence is reviewed. |

Write chosen values to `.env` from `.env.example`. Keep user-specific values in
`.env` or launchd environment, not in committed docs.

## Phase 1: Preflight The Checkout

1. Read the canonical machine rules:

   ```sh
   sed -n '1,240p' ~/.agents/AGENT.md
   ```

2. Inspect repository state and avoid unrelated changes:

   ```sh
   cd ~/Documents/Projects/ceo-agent-service
   git status --short --branch
   ```

3. Confirm Python and Pi-compatible Node are available. Pi requires Node
   `>=22.19.0`:

   ```sh
   python3 --version
   node --version
   npm --version
   ```

   Also confirm the sibling Pi checkout exists at `../pi`. The bootstrapper can
   build it when `packages/coding-agent/dist/cli.js` is absent.

4. Create or refresh the Python environment:

   ```sh
   python3 -m venv .venv
   .venv/bin/pip install -e '.[dev]'
   ```

5. Install Node dependencies only when package checks or Vercel/API tests are
   needed:

   ```sh
   npm install
   ```

If any dependency download is blocked by network, package registry, or missing
credentials, report the exact failed command and error.

## Phase 2: Download And Verify Components

Start with the local component bootstrapper. It installs the components whose
source is known on the machine and verifies the rest:

```sh
scripts/bootstrap-local-components.sh --format json
```

The bootstrapper automatically installs `terminal-notifier` through Homebrew
when available, discovers Node `>=22.19.0`, builds/verifies the sibling Pi CLI,
and verifies the Nvwa skill. DWS and Lark are
separate Tutorial steps because each CLI owns its installation and interactive
authorization lifecycle. If an internal component is missing, provide the
approved source through one of these environment variables and click its
Tutorial setup action:

- `DWS_INSTALLER_PATH`: executable installer for `dws`
- `DWS_INSTALL_COMMAND`: approved shell command for installing `dws`
- `LARK_CLI_INSTALL_COMMAND`: approved override for installing `lark-cli`
- `NVWA_SKILL_SOURCE`: approved local directory containing the Nvwa skill

Do not ask the user to copy individual terminal commands when the bootstrapper
can perform the step. Only interrupt the user for the approved source, login
approval, QR/browser authorization, macOS permission clicks, or live-send
decisions.

### dws

1. Check whether `dws` exists:

   ```sh
   command -v dws
   dws --version
   ```

2. If it exists, check for updates:

   ```sh
   dws upgrade --check --format json
   ```

3. If the update check says an upgrade is available, upgrade it:

   ```sh
   dws upgrade -y --format json
   ```

4. If `dws` is missing, set `DWS_INSTALLER_PATH` or `DWS_INSTALL_COMMAND` to
   the organization's approved installer or package command, then click
   `Tutorial -> DingTalk CLI -> Install or configure`. Do not invent a download
   URL.

5. Authenticate `dws`:

   ```sh
   dws auth status
   dws auth login
   dws doctor --json --timeout 5
   ```

The login step may require the user to approve a browser page, QR code, or
DingTalk prompt. The agent should initiate the flow and wait for the user's
confirmation instead of asking the user to run commands.

### Pi Agent

1. Confirm a compatible Node and the built sibling Pi CLI can run:

   ```sh
   node --version
   node ../pi/packages/coding-agent/dist/cli.js --version
   ```

2. If Node is older than `22.19.0`, install or select a compatible Node version,
   or set `CEO_PI_NODE_BINARY` to one. If the Pi CLI is not built, run the
   bootstrapper; it executes `npm ci --ignore-scripts` and `npm run build` in the
   sibling Pi checkout.

3. Configure provider, model, API protocol, Base URL, API Key, thinking level,
   Node path, Pi CLI path, agent directory, and session directory on
   `/config?tab=agent`. The API Key must stay in the ignored `.env` with mode
   `0600`; generated `models.json` contains only `$CEO_PI_API_KEY`. When Base
   URL is blank, the selected API protocol must match the built-in Pi model's
   actual protocol. Custom protocols or custom models normally require a
   trusted Base URL; the save handler verifies the resolved provider, model,
   protocol, and endpoint offline before writing configuration.

4. Confirm continuity support later through a dry-run worker pass; the service
   resumes local Pi sessions from the configured Pi session directory.

### macOS Notifications

The service prefers `terminal-notifier` for native macOS notifications and falls
back to browser notifications or `osascript` when unavailable. The bootstrapper
installs `terminal-notifier` automatically with Homebrew when possible:

```sh
scripts/bootstrap-local-components.sh --format json
```

### Reviewed integrations

The service ships a reviewed Friday Memory bridge. It is ready only when the
bridge file, Connector URL, and a locally stored copyable API key are all
present. The key is never rendered by the UI, and authenticated ACL owns memory
scope; never configure or pass `user_id`, `graph_id`, or `graph_ids`. DWS is
available only through the reviewed extension and installed schema metadata,
not through arbitrary bash. Friday Memory, Xiaoqing Interview, Exa, and Lark
use repository-owned reviewed adapters; missing OAuth, CLI login, or local
configuration is reported explicitly. An installed legacy Codex MCP entry by
itself does not grant Pi a capability.

### Nvwa Persona Skill

The Nvwa skill is needed for reviewed profile distillation, not for runtime:

```sh
test -f ~/.agents/skills/nvwa/SKILL.md
```

If it is missing, install or sync the approved internal skill package into
`~/.agents/skills/nvwa`. Generated profile content belongs in this repository,
not in `~/.agents/skills`.

## Phase 3: Configure The Service

1. Create `.env` if absent:

   ```sh
   cp .env.example .env
   ```

   The app loads `CEO_ENV_FILE` automatically. If `CEO_ENV_FILE` is unset, it
   reads this repository's `.env`.

2. Edit `.env` with the Phase 0 values, then enforce private permissions without
   displaying the file contents:

   ```sh
   chmod 600 .env
   ```

   Minimum fields to set:

   ```text
   CEO_WORKSPACE=$HOME/Documents/memory
   CEO_WORKER_DB=$HOME/Library/Application Support/ceo-agent-service/auto-reply.sqlite3
   CEO_CORPUS_DIR=./data/corpus
   CEO_PI_NODE_BINARY=
   CEO_PI_CLI_PATH=../pi/packages/coding-agent/dist/cli.js
   CEO_PI_PROVIDER=deepseek
   CEO_PI_MODEL=deepseek-v4-pro
   CEO_PI_API=openai-completions
   CEO_PI_BASE_URL=
   CEO_PI_API_KEY=<provider API key>
   CEO_PI_THINKING_LEVEL=medium
   CEO_PI_AGENT_DIR=$HOME/Library/Application Support/ceo-agent-service/pi-agent
   CEO_PI_SESSION_DIR=$HOME/Library/Application Support/ceo-agent-service/pi-sessions
   CEO_DRY_RUN=1
   CEO_PRINCIPAL_NAME=<principal display name>
   USER_ALIAS=<principal display name>
   CEO_MENTION_ALIASES=<comma-separated DingTalk @ aliases>
   DOCUMENT_EXTRACTION_IDS=<names used in docs and prompts>
   CEO_ASSISTANT_SIGNATURE=<signature>
   CEO_HANDOFF_ACK=<handoff acknowledgement>
   CEO_LIVE_SEND_BLOCKERS_ACCEPTED=
   ```

3. Keep dry-run on for first validation. For this codebase, dry-run can be set
   as either `CEO_DRY_RUN=1`, `CEO_NOT_SEND_MESSAGE=1`, or the CLI
   `--dry-run` flag. The checked-in launchd entry and installation script now
   default to `CEO_SERVICE_MODE=dry-run`; live mode requires a separate,
   explicit opt-in.

4. Verify important paths exist:

   ```sh
   mkdir -p data/corpus "$HOME/Documents/memory"
   test -d "$HOME/Documents/memory"
   ```

## Phase 4: Prepare Data Corpus

The workspace should contain readable local materials. Recommended shape:

```text
~/Documents/memory/
├── AI听记/
├── management/
│   ├── OA/
│   └── strategy/
├── recruiting/
├── Thinking/
└── graphify-out/
```

Agent tasks:

1. Confirm `AI听记` and key SOP folders exist. If missing, ask where the user's
   meeting notes, SOPs, HR/recruiting docs, and strategy docs live.
2. Do not move private files into Git. Keep them under `CEO_WORKSPACE` or another
   ignored local data path.
3. Build the local AI-minutes style corpus:

   ```sh
   .venv/bin/ceo-agent build-corpus \
     --workspace "$HOME/Documents/memory" \
     --corpus-dir ./data/corpus
   ```

4. Append recent DingTalk sent-message samples:

   ```sh
   .venv/bin/ceo-agent collect-corpus \
     --workspace "$HOME/Documents/memory" \
     --corpus-dir ./data/corpus
   ```

This reads through the current `dws` identity. If the command fails on auth or
permission, fix `dws` before continuing.

## Phase 5: Generate And Review The Work Profile

1. Build the initial profile and evidence index:

   ```sh
   .venv/bin/ceo-agent build-work-profile \
     --workspace "$HOME/Documents/memory" \
     --corpus-dir ./data/corpus
   ```

2. If the user provided a DingTalk KB workspace id or URL, include it:

   ```sh
   .venv/bin/ceo-agent build-work-profile \
     --workspace "$HOME/Documents/memory" \
     --corpus-dir ./data/corpus \
     --dingtalk-kb-workspace '<workspace-id-or-url>'
   ```

3. Expected outputs:

   ```text
   data/work-profile/work_profile.md
   data/profile-evidence/evidence_index.jsonl
   data/corpus/style_corpus.csv
   ```

4. Run a Nvwa review pass over:

   ```text
   data/work-profile/work_profile.md
   data/profile-evidence/evidence_index.jsonl
   data/corpus/style_corpus.csv
   ```

5. The Nvwa pass must rewrite only `data/work-profile/work_profile.md`. It must not add
   raw private excerpts, absolute local paths, tokens, session ids, or DingTalk
   cache content.

6. Verify runtime consumption:

   ```sh
   .venv/bin/pytest \
     tests/test_work_profile.py \
     tests/test_prompt.py \
     tests/test_worker.py::test_consumer_pi_command_injects_work_profile_content \
     -q
   ```

Runtime reads the profile through `app.prompt:work_profile_instruction()`.

## Phase 6: Validate dws Permissions

Run read probes first:

```sh
.venv/bin/ceo-agent probe-dws
dws auth status
dws doctor --json --timeout 5
```

For known online docs or AI tables, validate access by type:

```sh
dws doc info --node '<alidocs-url>' --format json
dws doc read --node '<alidocs-url>' --format json
```

Permissions to verify before live operation:

- DingTalk login and `dws` keychain state are available under the real user
  account.
- The agent can read unread conversations, group context, quoted messages, docs,
  AI tables, contacts, calendar items, OA materials, and AI minutes needed by the
  deployment.
- macOS allows the Pi/Node process and Terminal access needed for local files and network.
- Notifications are allowed if macOS notifications are part of the deployment.
- The service can bind the local audit web port, usually `127.0.0.1:8765`.
- OA approval actions and chat sends remain blocked until explicit live-send
  opt-in is reviewed.

If a required permission is unavailable, record the exact missing capability and
whether the right fix is user authorization, DingTalk admin scope, or a narrower
deployment boundary.

## Phase 7: Start Web Management In Dry-Run

1. Start the audit web UI:

   ```sh
   .venv/bin/python -m app.cli audit-web \
     --reload \
     --host 127.0.0.1 \
     --port 8765
   ```

2. Open and inspect:

   ```text
   http://127.0.0.1:8765/
   ```

3. Key pages:

   - `/`: reply history and pending tasks.
   - `/attempts/{id}`: single attempt, prompt, decision, evidence, send status.
   - `/tasks`: project/TODO summary and follow-up drafts.
   - `/pi`: local Pi session references. `/codex` only redirects legacy links.
   - `/developer-prompt`: prompt templates.
   - `/config`: routing rules and runtime config.
   - `/errors`: unresolved runtime errors.

4. Run one dry-run pass:

   ```sh
   CEO_NOT_SEND_MESSAGE=1 .venv/bin/ceo-agent run-once --not-send-message
   ```

5. Review the web UI for:

   - no unresolved `processing` or `failed` backlog
   - no leaked local paths, tokens, session ids, or raw tool output
   - correct routing for group @, single chat, OA, docs, calendar, and permission
     request cases
   - no unexpected live send

## Phase 8: Install launchd Service

Install launchd only after dry-run behavior and configuration are reviewed.

1. Inspect `launchd/com.ceo-agent-service.main.plist`. Confirm service root,
   workspace, DB, corpus path, principal/persona variables, and live-send
   defaults match the deployment.

2. Choose a port and an absolute SQLite path. For acceptance testing, use a
   separate port and database so the service cannot consume an existing
   production queue. The installer defaults to dry-run and writes these
   values into the installed user LaunchAgent.

3. Install:

   ```sh
   scripts/install-auto-reply-agents.sh \
     --dry-run \
     --port 8766 \
     --db "$HOME/Library/Application Support/ceo-agent-service/pi-acceptance.sqlite3"
   ```

4. Verify:

   ```sh
   launchctl print gui/$(id -u)/com.ceo-agent-service.main | sed -n '1,80p'
   curl -fsS http://127.0.0.1:8766/ >/tmp/ceo-agent-home.html
   ```

5. Check logs:

   ```sh
   ls -lah ~/Library/Logs/ceo-agent-service
   ```

6. Check the audit UI for unresolved failures or stuck tasks before reporting
   completion.

## Phase 9: Optional Live Send Enablement

Only after reviewing dry-run attempts with the user:

1. Confirm the exact live scope: which chats, which aliases, which actions, and
   whether OA/calendar/task follow-up actions are allowed.
2. Set the explicit live acceptance gate and reinstall in live mode:

   ```sh
   CEO_LIVE_SEND_BLOCKERS_ACCEPTED=1 \
     scripts/install-auto-reply-agents.sh \
       --live \
       --port 8765 \
       --db "$HOME/Library/Application Support/ceo-agent-service/auto-reply.sqlite3"
   ```

3. Restart launchd if runtime service behavior changed:

   ```sh
   launchctl kickstart -k gui/$(id -u)/com.ceo-agent-service.main
   launchctl print gui/$(id -u)/com.ceo-agent-service.main | sed -n '1,80p'
   ```

4. Send one controlled test through the UI or a reviewed attempt:

   ```sh
   CEO_NOT_SEND_MESSAGE=0 CEO_LIVE_SEND_BLOCKERS_ACCEPTED=1 \
     .venv/bin/ceo-agent send-attempt --attempt-id <reviewed-attempt-id>
   ```

5. Re-check `/attempts/{id}`, `/errors`, and recent DingTalk state.

## Completion Checklist

- Shared rules read from `~/.agents/AGENT.md`.
- Worktree inspected and unrelated changes preserved.
- Python environment installed and tests for touched behavior pass.
- `dws` exists, is authenticated, and passes `probe-dws`.
- Node `>=22.19.0` and the sibling Pi CLI exist and can be used by the worker.
- The reviewed Pi extension and DWS schema probe pass; Friday Memory is either
  configured and ready or explicitly marked Config Missing without exposing a key.
- Exa reports reviewed read-only readiness; Xiaoqing reports its local OAuth
  state; Lark reports official CLI/schema readiness; Nvwa reports whether its
  local skill source is installed. No legacy Codex MCP setup is assumed to
  transfer.
- `.env` contains deployment-specific values, has mode `0600`, and remains uncommitted.
- Workspace and corpus directories exist outside committed source data.
- `build-corpus`, `collect-corpus`, and `build-work-profile` completed or have
  documented blockers.
- `data/work-profile/work_profile.md` reviewed and contains no private raw evidence.
- Audit web UI loads on `127.0.0.1:8765`.
- Dry-run `run-once` has been reviewed in the UI.
- launchd is installed only after dry-run approval.
- No unresolved `failed` or `processing` backlog remains.
- Live send is disabled unless the user explicitly approved it.
