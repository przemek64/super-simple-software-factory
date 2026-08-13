# Audit Task

## Variables

### prompt

{{prompt}}

### context_handoff_dir

{{context_handoff_dir}}

### work_root

{{work_root}}

## Task

Do exactly what `prompt` describes: read the first reviewer's findings for the named file, read the file and its diff yourself, then report NET-NEW misses and REBUTTALS to the path it names. Work inside `work_root`.

## Report

Respond with ONLY valid JSON matching `GenericOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<one sentence: N net-new findings, M pass-1 findings refuted, on <file>>",
  "artifacts": ["<the audit file you wrote>"],
  "notes_for_next_agent": "<what the final summary must not lose from this audit>"
}
```

`status` is `success` when the audit completed and the audit file is on disk — including when the honest result is that pass 1 was right and complete.
