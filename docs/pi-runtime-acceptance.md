# Pi Runtime Acceptance Guide

This guide verifies the migration boundary: the Friday service and its
existing product behavior stay in place while Pi Agent replaces Codex as the
runtime. It is not a generic demo checklist. A pass requires both runtime
evidence and feature-parity evidence.

## Acceptance Build

Install the supervised test service with a dedicated port and database:

```sh
scripts/install-auto-reply-agents.sh \
  --dry-run \
  --port 8766 \
  --db "$HOME/Library/Application Support/ceo-agent-service/pi-acceptance.sqlite3"
```

This mode may read authenticated sources to produce realistic decisions, but
it must not send messages or perform reviewed external writes. It does not
consume tasks from the normal `auto-reply.sqlite3` database.

Open these pages:

- `http://127.0.0.1:8766/workers`: supervised service state and queue health.
- `http://127.0.0.1:8766/config?tab=agent`: Pi Provider, Model, API protocol,
  Base URL, API Key, thinking level, Node binary, and Pi CLI configuration.
- `http://127.0.0.1:8766/config?tab=channels`: DingTalk/Lark channel gates and
  account authorization state.
- `http://127.0.0.1:8766/pi`: Pi sessions and runtime evidence.
- `http://127.0.0.1:8766/`: history, attempts, decisions, receipts, and errors.

The pre-migration `/codex` URL is retained only as a compatibility redirect to
`/pi`. Existing Codex history remains readable; new executions must create Pi
session evidence.

## Required Checks

### 1. Runtime replacement

- The Agent configuration page resolves the sibling `../pi` CLI with Node
  `22.19.0+`.
- Provider validation succeeds without exposing the API Key in HTML, logs,
  command arguments, Pi `models.json`, or session transcripts.
- A new dry-run attempt records a Pi session and Pi transcript range.
- New runtime failures and traces refer to Pi, not a Codex subprocess.
- `/codex` redirects to `/pi`; old stored Codex session identifiers remain
  read-only historical data and are not rewritten.

### 2. Custom Provider configuration

- Provider, model, API protocol, Base URL, API Key, and thinking level can be
  saved independently from the source tree.
- Base URL accepts only an absolute HTTP(S) URL without embedded credentials,
  query, or fragment.
- Leaving the API Key field blank preserves the existing secret. The UI never
  echoes the stored key.
- The resolved provider/model/protocol/endpoint is validated by the Pi model
  resolver before the configuration is accepted.
- A real Provider call succeeds using the saved local configuration.

### 3. Existing Friday behavior on Pi

Run or inspect representative dry-run attempts for each applicable path:

| Capability | Acceptance evidence |
| --- | --- |
| DingTalk private/group routing | private chat does not require @; group chat follows @/broadcast alias rules; task identity stays channel-scoped |
| Documents, OA, calendar, meetings, TODO, mail and other DWS reads | reviewed DWS schema is ready and Pi receives only registered reviewed tools |
| Reply decision and delivery gates | decision, evidence and proposed reply are stored; dry-run never claims or sends delivery |
| Memory | reviewed `memory_recall` is available; writes are exposed only to the dedicated reviewed writer |
| Exa | only `web_search_exa` and `web_fetch_exa` are available; private/localhost/credential-bearing URLs are rejected |
| Xiaoqing | five reviewed reads and the guarded upload contract are registered together; real calls require local OAuth |
| Lark | official CLI schema is reviewed; read/write adapters coexist; login, config, installation and high-risk commands remain blocked inside Pi |
| Graphify | `query`, `explain`, and `path` work through the read-only adapter without a shell fallback |
| NvWa | work-profile review can atomically replace only the fixed profile output; ordinary runs do not receive its write tool |
| Images and attachments | DingTalk image download and reviewed attachment paths remain available without leaking signed URLs or local paths |
| History, feedback and audit | attempts, tool events, decisions, receipts, feedback and errors remain visible in the existing Friday UI |
| Recovery and reconciliation | unknown writes are not replayed; read-only reconciliation requires matching operation/target/result proof |
| Concurrent capabilities | DWS, Memory, Exa, Xiaoqing, Lark, Graphify and approved runtime tools are registered in one Pi extension rather than mutually exclusive modes |

### 4. Health and failure behavior

Run:

```sh
.venv/bin/ceo-agent doctor-mcp --verify-live
launchctl print gui/$(id -u)/com.ceo-agent-service.main | sed -n '1,80p'
```

Expected service conditions:

- launchd state is `running` with a current PID.
- `/workers` reports the main service as running.
- The acceptance database has no unresolved `processing`, `failed`, or
  unknown-side-effect backlog before handoff.
- Graphify, NvWa, Memory, Exa and reviewed DWS capabilities report ready when
  their local installations and credentials are present.
- Missing account authorization is reported as `missing_auth` or `needs_login`;
  it must not be reported as an implementation success.

## Account-dependent Checks

Installation and implementation do not create external account credentials.
These checks require the tester or deployment owner to authorize locally:

- Lark: install `lark-cli`, then complete its local interactive login. The
  project reuses that CLI login state and does not copy its credentials.
- Xiaoqing: configure `CEO_PI_XIAOQING_ACCESS_TOKEN` locally. Do not paste the
  token into chat, a ticket, a screenshot, or a committed file.
- Live DingTalk/Lark sends and other external writes: require explicit test
  targets plus the live-send gates. They are intentionally not exercised by
  this dry-run build.

An account-dependent row is accepted only after a real authenticated probe or
call succeeds. A mocked contract test proves adapter behavior but does not
prove the external account is authorized.

## Pass Criteria

The build passes when:

1. New agent executions demonstrably use Pi and the configured Provider.
2. Provider/API Key/Base URL configuration is usable and secret-safe.
3. Every previous Codex-facing Friday capability is either demonstrated on Pi
   or has exactly one documented external-account authorization blocker.
4. Multiple external capabilities are available together in the Pi extension.
5. Existing UI, prompts, memory boundaries, history, receipts, feedback,
   routing, retries, reconciliation, and legacy history remain intact.
6. The supervised acceptance service is healthy and its isolated database has
   no unresolved failure or processing backlog.

Do not mark the migration accepted solely because the configuration page says
`Provider configured`, or because a mocked test suite passes. At least one
real Pi Provider call and the relevant authenticated capability probes are
required.
