#!/usr/bin/env bash
set -u

FORMAT_TEXT=1
if [[ "${1:-}" == "--format" && "${2:-}" == "json" ]]; then
  FORMAT_TEXT=0
fi

RESULTS=()
FAILED=0
OPTIONAL_MISSING=0

record() {
  local component="$1"
  local status="$2"
  local detail="$3"
  RESULTS+=("${component}"$'\t'"${status}"$'\t'"${detail}")
  if [[ "${status}" == "failed" ]]; then
    FAILED=1
  elif [[ "${status}" == "optional_missing" ]]; then
    OPTIONAL_MISSING=1
  fi
  if [[ "${FORMAT_TEXT}" == "1" ]]; then
    printf '%s: %s - %s\n' "${component}" "${status}" "${detail}"
  fi
}

json_string() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()), end="")'
}

emit_json() {
  local summary
  if [[ "${FAILED}" == "0" ]]; then
    if [[ "${OPTIONAL_MISSING}" == "1" ]]; then
      summary="Required local CLI components are ready; optional preparation components still need local sources."
    else
      summary="Local CLI components were checked and repaired."
    fi
  else
    summary="Some CLI components still need an approved installer or manual authorization."
  fi

  printf '{"status":'
  if [[ "${FAILED}" == "0" ]]; then
    printf '"done"'
  else
    printf '"failed"'
  fi
  printf ',"summary":'
  printf '%s' "${summary}" | json_string
  printf ',"components":['
  local first=1
  local row component status detail
  for row in "${RESULTS[@]}"; do
    IFS=$'\t' read -r component status detail <<< "${row}"
    if [[ "${first}" == "0" ]]; then
      printf ','
    fi
    first=0
    printf '{"name":'
    printf '%s' "${component}" | json_string
    printf ',"status":'
    printf '%s' "${status}" | json_string
    printf ',"detail":'
    printf '%s' "${detail}" | json_string
    printf '}'
  done
  printf ']}\n'
}

command_path() {
  command -v "$1" 2>/dev/null || true
}

short_version() {
  "$@" 2>/dev/null | head -n 1 || true
}

install_with_command() {
  local command_var="$1"
  local configured_command="${!command_var:-}"
  if [[ -z "${configured_command}" ]]; then
    return 1
  fi
  bash -lc "${configured_command}"
}

ensure_terminal_notifier() {
  local path
  path="$(command_path terminal-notifier)"
  if [[ -n "${path}" ]]; then
    record "terminal-notifier" "done" "available at ${path}"
    return
  fi

  local brew
  brew="$(command_path brew)"
  if [[ -z "${brew}" ]]; then
    record "terminal-notifier" "failed" "Homebrew is missing; install Homebrew or install terminal-notifier from an approved package."
    return
  fi

  if "${brew}" install terminal-notifier; then
    path="$(command_path terminal-notifier)"
    if [[ -n "${path}" ]]; then
      record "terminal-notifier" "done" "installed with Homebrew at ${path}"
    else
      record "terminal-notifier" "failed" "Homebrew finished but terminal-notifier is still not on PATH."
    fi
  else
    record "terminal-notifier" "failed" "Homebrew install terminal-notifier failed."
  fi
}

node_version_ok() {
  local binary="$1"
  [[ -x "${binary}" ]] || return 1
  "${binary}" -e '
    const [major, minor, patch] = process.versions.node.split(".").map(Number);
    process.exit(
      major > 22 ||
      (major === 22 && (minor > 19 || (minor === 19 && patch >= 0)))
        ? 0 : 1
    );
  ' >/dev/null 2>&1
}

find_pi_node() {
  local candidate
  if [[ -n "${CEO_PI_NODE_BINARY:-}" ]] && node_version_ok "${CEO_PI_NODE_BINARY}"; then
    printf '%s\n' "${CEO_PI_NODE_BINARY}"
    return
  fi
  candidate="$(command_path node)"
  if [[ -n "${candidate}" ]] && node_version_ok "${candidate}"; then
    printf '%s\n' "${candidate}"
    return
  fi
  while IFS= read -r candidate; do
    if node_version_ok "${candidate}"; then
      printf '%s\n' "${candidate}"
      return
    fi
  done < <(find "${HOME}/.nvm/versions/node" -path '*/bin/node' -type f 2>/dev/null | sort -Vr)
}

ensure_pi() {
  local script_root repo_root pi_root cli_path node_path node_version npm_path
  script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo_root="$(cd "${script_root}/.." && pwd)"
  pi_root="$(cd "${repo_root}/.." && pwd)/pi"
  cli_path="${CEO_PI_CLI_PATH:-${pi_root}/packages/coding-agent/dist/cli.js}"
  node_path="$(find_pi_node)"
  if [[ -z "${node_path}" ]]; then
    record "pi-node" "failed" "Pi requires Node >=22.19.0; set CEO_PI_NODE_BINARY or install a compatible Node version."
    return
  fi
  node_version="$(short_version "${node_path}" --version)"
  record "pi-node" "done" "available at ${node_path}${node_version:+ (${node_version})}"

  if [[ -f "${cli_path}" ]]; then
    record "pi-agent" "done" "built CLI available at ${cli_path}"
    return
  fi
  if [[ ! -f "${pi_root}/package.json" ]]; then
    record "pi-agent" "failed" "Missing sibling Pi checkout at ${pi_root}."
    return
  fi
  npm_path="$(dirname "${node_path}")/npm"
  if [[ ! -x "${npm_path}" ]]; then
    record "pi-agent" "failed" "Compatible npm is missing next to ${node_path}."
    return
  fi
  if (
    cd "${pi_root}" &&
    PATH="$(dirname "${node_path}"):${PATH}" "${npm_path}" ci --ignore-scripts &&
    PATH="$(dirname "${node_path}"):${PATH}" "${npm_path}" run build
  ); then
    if [[ -f "${cli_path}" ]]; then
      record "pi-agent" "done" "built sibling Pi CLI at ${cli_path}"
      return
    fi
  fi
  record "pi-agent" "failed" "Pi build did not produce ${cli_path}."
}

copy_nvwa_source() {
  local source="$1"
  local target="${HOME}/.agents/skills/nvwa"
  mkdir -p "${target}"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "${source%/}/" "${target}/"
  else
    cp -R "${source%/}/." "${target}/"
  fi
}

ensure_nvwa() {
  local primary="${HOME}/.agents/skills/nvwa/SKILL.md"
  local legacy_nuwa="${HOME}/.agents/skills/nuwa/SKILL.md"
  local legacy_huashu="${HOME}/.agents/skills/huashu-nuwa/SKILL.md"
  if [[ -f "${primary}" || -f "${legacy_nuwa}" || -f "${legacy_huashu}" ]]; then
    record "nvwa-skill" "done" "Nvwa skill is available."
    return
  fi

  if [[ -n "${NVWA_SKILL_SOURCE:-}" && -d "${NVWA_SKILL_SOURCE}" ]]; then
    if copy_nvwa_source "${NVWA_SKILL_SOURCE}"; then
      if [[ -f "${primary}" ]]; then
        record "nvwa-skill" "done" "installed from NVWA_SKILL_SOURCE."
        return
      fi
      record "nvwa-skill" "failed" "NVWA_SKILL_SOURCE copied but SKILL.md is missing."
      return
    fi
    record "nvwa-skill" "failed" "Failed to copy NVWA_SKILL_SOURCE."
    return
  fi

  record "nvwa-skill" "optional_missing" "Optional profile-distillation skill is absent; set NVWA_SKILL_SOURCE when a reviewed local source is available. Runtime Pi is unaffected."
}

ensure_terminal_notifier
ensure_pi
ensure_nvwa

if [[ "${FORMAT_TEXT}" == "0" ]]; then
  emit_json
fi

exit "${FAILED}"
