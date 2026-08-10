# Direct Agent Session Serialization

Each Direct Agent run acquires the durable `codex_session_locks` row for its
conversation before creating an Agent run or starting Pi. The table retains its
legacy name for database compatibility. A second task for that conversation
returns to `pending` with a short delay and does not consume a business retry.
This prevents concurrent Pi `--session-id` invocations from writing to the same
Pi session.

If the service stops mid-run, the existing stale-lock expiry permits the
persisted task to resume. Nonzero Pi exits retain a redacted stderr summary in
the Agent run's structured error; prompts, command arguments, API Keys, and
credentials are not stored in that diagnostic field.

Direct Agent results are validated locally after Pi exits. Pi does not support
Codex `--output-schema`; structured runners embed the effective JSON Schema in
the system prompt, validate the returned object with Pydantic, and request one
repair turn in the same Pi session when validation fails.

Pi is configured with `--thinking` and the selected `CEO_PI_THINKING_LEVEL`.
Provider failures cannot be detected from the process exit code alone because
Pi can emit a JSON `message_end` or `auto_retry_end` error and still exit zero.
The service parses these events and maps them to
`pi_provider_auth_failed`, `pi_provider_unavailable`, or `pi_process_failed`.

Unknown external side effects are never replayed automatically. Reconciliation
runs under the same conversation/session lock with reviewed read tools only. It
must bind the original operation digest and target identifiers to one matching
read receipt and proof before returning confirmed or absent. Missing,
ambiguous, provider-failed, or lifecycle-incomplete evidence keeps
`side_effect_state=unknown` and schedules another bounded retry; a write tool in
reconciliation is rejected.
