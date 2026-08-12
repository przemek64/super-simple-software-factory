# Triage Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Rule on every CodeRabbit finding, before any code is repaired.

1. Read the findings file named in `prompt` (`findings_path`). Each finding has an id in square brackets — that id is what you rule on.
2. For each finding, read the code it points at and decide whether it names a real problem in this codebase.
3. Emit one verdict per finding: `accepted` true or false. Every rejection carries a one-sentence `reason`.
4. Change nothing. The builder makes the repairs, from your decisions.

## Report

Respond with ONLY valid JSON matching `TriageOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<one sentence: N of M findings accepted>",
  "verdicts": [
    { "finding_id": "a1b2c3d4e5f6", "accepted": true, "reason": "" },
    { "finding_id": "0f9e8d7c6b5a", "accepted": false, "reason": "the annotation is already correct at config_reader.py:374 — the finding misreads the type" }
  ],
  "artifacts": [],
  "notes_for_next_agent": "<what the builder should watch out for while fixing the accepted findings>"
}
```

`status` is `success` when the triage itself completed — it is not a verdict on the findings. Every `finding_id` in the findings file must appear exactly once, and no other id may appear.
