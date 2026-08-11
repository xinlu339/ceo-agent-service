#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_dir="${HOME}/Library/LaunchAgents"
log_dir="${HOME}/Library/Logs/ceo-agent-service"
domain="gui/$(id -u)"
service_mode="dry-run"
service_port="${CEO_AUDIT_WEB_PORT:-8765}"
service_db="${CEO_WORKER_DB:-${HOME}/Library/Application Support/ceo-agent-service/auto-reply.sqlite3}"

usage() {
  cat <<'EOF'
Usage: scripts/install-auto-reply-agents.sh [options]

Options:
  --dry-run       Install the service without external writes or message sends (default).
  --live          Install live mode; requires CEO_LIVE_SEND_BLOCKERS_ACCEPTED=1.
  --port PORT     Audit Web port written to the installed launchd plist.
  --db PATH       SQLite path written to the installed launchd plist.
  -h, --help      Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      service_mode="dry-run"
      shift
      ;;
    --live)
      service_mode="live"
      shift
      ;;
    --port)
      [[ $# -ge 2 ]] || { echo "--port requires a value" >&2; exit 64; }
      service_port="$2"
      shift 2
      ;;
    --db)
      [[ $# -ge 2 ]] || { echo "--db requires a value" >&2; exit 64; }
      service_db="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

[[ "${service_port}" =~ ^[0-9]+$ ]] || { echo "--port must be numeric" >&2; exit 64; }
(( service_port >= 1 && service_port <= 65535 )) || { echo "--port is out of range" >&2; exit 64; }
[[ "${service_db}" = /* ]] || { echo "--db must be an absolute path" >&2; exit 64; }
if [[ "${service_mode}" == "live" && "${CEO_LIVE_SEND_BLOCKERS_ACCEPTED:-}" != "1" ]]; then
  echo "--live requires CEO_LIVE_SEND_BLOCKERS_ACCEPTED=1" >&2
  exit 64
fi

legacy_label_prefix="com.$(id -un).ceo-agent-service"
plist_names=(
  "com.ceo-agent-service.main.plist"
)
obsolete_labels=(
  "com.ceo-agent-service.reply-producer"
  "com.ceo-agent-service.reply-consumer"
  "com.ceo-agent-service.audit-web"
)
legacy_labels=(
  "${legacy_label_prefix}.reply-producer"
  "${legacy_label_prefix}.reply-consumer"
  "${legacy_label_prefix}.audit-web"
  "${legacy_label_prefix}.hourly-dry-run"
  "${legacy_label_prefix}.dry-run-consumer"
  "${legacy_label_prefix}.memory-flush"
)
obsolete_plist_names=(
  "com.ceo-agent-service.reply-producer.plist"
  "com.ceo-agent-service.reply-consumer.plist"
  "com.ceo-agent-service.audit-web.plist"
)
legacy_plist_names=(
  "${legacy_label_prefix}.reply-producer.plist"
  "${legacy_label_prefix}.reply-consumer.plist"
  "${legacy_label_prefix}.audit-web.plist"
  "${legacy_label_prefix}.hourly-dry-run.plist"
  "${legacy_label_prefix}.dry-run-consumer.plist"
  "${legacy_label_prefix}.memory-flush.plist"
)

mkdir -p "${target_dir}" "${log_dir}"
mkdir -p "$(dirname "${service_db}")"

set_plist_string() {
  local plist_path="$1"
  local key_path="$2"
  local value="$3"
  if ! plutil -replace "${key_path}" -string "${value}" "${plist_path}" \
    >/dev/null 2>&1; then
    plutil -insert "${key_path}" -string "${value}" "${plist_path}"
  fi
}

for label in "${obsolete_labels[@]}"; do
  launchctl bootout "${domain}/${label}" 2>/dev/null || true
done
for label in "${legacy_labels[@]}"; do
  launchctl bootout "${domain}/${label}" 2>/dev/null || true
done
for plist_name in "${obsolete_plist_names[@]}"; do
  rm -f "${target_dir}/${plist_name}"
done
for plist_name in "${legacy_plist_names[@]}"; do
  rm -f "${target_dir}/${plist_name}"
done

for plist_name in "${plist_names[@]}"; do
  label="${plist_name%.plist}"
  source_plist="${repo_root}/launchd/${plist_name}"
  target_plist="${target_dir}/${plist_name}"

  cp "${source_plist}" "${target_plist}"
  # The checked-in plist remains portable. Pin the installed service to this
  # checkout so launchd does not depend on its sparse environment or a
  # machine-specific default repository location.
  set_plist_string "${target_plist}" EnvironmentVariables.CEO_SERVICE_ROOT "${repo_root}"
  set_plist_string "${target_plist}" EnvironmentVariables.CEO_SERVICE_MODE "${service_mode}"
  set_plist_string "${target_plist}" EnvironmentVariables.CEO_AUDIT_WEB_PORT "${service_port}"
  set_plist_string "${target_plist}" EnvironmentVariables.CEO_WORKER_DB "${service_db}"
  plutil -lint "${target_plist}" >/dev/null

  launchctl bootout "${domain}/${label}" 2>/dev/null || true
  launchctl bootout "${domain}" "${target_plist}" 2>/dev/null || true
  launchctl bootstrap "${domain}" "${target_plist}"
  launchctl kickstart -k "${domain}/${label}"

  printf 'installed %s mode=%s port=%s db=%s\n' \
    "${target_plist}" "${service_mode}" "${service_port}" "${service_db}"
done
