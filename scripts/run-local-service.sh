#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export HOME="${CEO_SERVICE_HOME:-${HOME}}"
export PYTHONPATH="${PYTHONPATH:-.}"
export CEO_PI_CLI_PATH="${CEO_PI_CLI_PATH:-${repo_root}/../pi/packages/coding-agent/dist/cli.js}"
export CEO_PI_PROVIDER="${CEO_PI_PROVIDER:-deepseek}"
export CEO_PI_MODEL="${CEO_PI_MODEL:-deepseek-v4-pro}"
export CEO_PI_API="${CEO_PI_API:-openai-completions}"
export CEO_PI_THINKING_LEVEL="${CEO_PI_THINKING_LEVEL:-medium}"
export CEO_PI_AGENT_DIR="${CEO_PI_AGENT_DIR:-${HOME}/Library/Application Support/ceo-agent-service/pi-agent}"
export CEO_PI_SESSION_DIR="${CEO_PI_SESSION_DIR:-${HOME}/Library/Application Support/ceo-agent-service/pi-sessions}"
export CEO_WORKSPACE="${CEO_WORKSPACE:-${HOME}/Documents/memory}"
export CEO_DING_ROBOT_NAME="${CEO_DING_ROBOT_NAME:-磊哥}"
export CEO_WORKER_DB="${CEO_WORKER_DB:-${repo_root}/data/auto-reply.sqlite3}"
export CEO_NOT_SEND_MESSAGE="${CEO_NOT_SEND_MESSAGE:-${CEO_DRY_RUN:-0}}"
export CEO_CORPUS_DIR="${CEO_CORPUS_DIR:-${repo_root}/data/corpus}"
export CEO_OKR_LIVE_SOURCE_COMMAND="${CEO_OKR_LIVE_SOURCE_COMMAND:-${repo_root}/.venv/bin/python ${HOME}/.agents/skills/dingtang-okr-review/scripts/dingteam_okr_browser_source.py fetch --user-id {user_id} --period-label {period_label}}"

ceo_agent_cmd=(.venv/bin/python -c 'from app.cli import main; main()')
if [[ -x .venv/bin/ceo-agent ]]; then
  ceo_agent_cmd=(.venv/bin/ceo-agent)
fi

if [[ -n "${CEO_MAX_BATCHES:-}" ]]; then
  exec "${ceo_agent_cmd[@]}" run-once --max-batches "${CEO_MAX_BATCHES}"
fi

exec "${ceo_agent_cmd[@]}" service \
  --host "${CEO_AUDIT_WEB_HOST:-127.0.0.1}" \
  --port "${CEO_AUDIT_WEB_PORT:-8765}"
