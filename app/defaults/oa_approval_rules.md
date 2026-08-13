# DingTalk OA conservative approval rules

These are the safe fallback rules used only when the configured, process-specific approval rule file is unavailable.

1. Read the live approval detail first. Base every conclusion on the current form, appendices, comments, approval nodes, attachments, and linked business material. Never infer a missing fact from the notification title alone.
2. This fallback is not authority to approve an application. Approve only when the live material itself contains an explicit applicable SOP/rule, all required evidence is available, and the application clearly satisfies that rule.
3. If an applicable rule is missing, evidence cannot be read, a required field is absent, or the conclusion is uncertain, keep the approval pending and return `needs_human` with the exact missing fact. When writes are authorized, leave a factual comment and notify the actual applicant of the next material or action required.
4. If the material clearly conflicts with an explicit applicable rule, request a return with the concrete mismatch. Never use rejection as a substitute when the available DingTalk capability cannot perform a true return.
5. Never approve, reject, return, comment, or notify based on guessed identifiers. Use the process instance, task, applicant, and live read-back evidence returned by the reviewed DingTalk tools.
6. Do not repeat equivalent reads or broaden into workspace, Memory, web, or unrelated searches. If the deterministic evidence path is insufficient, stop safely with `needs_human`.
