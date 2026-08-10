# Pi session history lookup

The reply worker stores Pi transcript line offsets so review pages can show
which files and commands were used for one decision. Runtime lookup only scans
the dedicated Pi session directory.

## Request path

Session lookup uses this order:

1. Validate the requested session ID against the safe ID pattern.
2. Scan only `CEO_PI_SESSION_DIR` for `*.jsonl` files.
3. Read the leading Pi `{"type":"session","id":"..."}` header and require an
   exact ID match.
4. If no exact header match exists, return missing.

The request path never scans unrelated user directories or the old global
Codex history.

## Session contents

Pi session files contain:

- a versioned session header with `id`, `timestamp`, and `cwd`;
- persisted `message` entries for user, assistant, and reviewed tool results;
- assistant `toolCall` blocks and tool results used to render audit cards.

## Reading transcripts

Line counts stream the file line by line. Audit extraction reads only the
requested line range with streaming iteration instead of loading the whole
transcript into memory.

The Web UI is available at `/pi` and `/pi/{session_id}`. Legacy `/codex` routes
redirect to the Pi pages.
