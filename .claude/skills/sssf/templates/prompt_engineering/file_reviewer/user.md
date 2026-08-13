# File Review Task

## Variables

### prompt

{{prompt}}

### context_handoff_dir

{{context_handoff_dir}}

### work_root

{{work_root}}

## Task

Do exactly what `prompt` describes: review the one named file, both framings, every aspect, and write your findings to the path it names. Work inside `work_root`.

## Report

Respond with ONLY valid JSON matching `GenericOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<one sentence: N findings on <file>, highest severity X — or 'no findings'>",
  "artifacts": ["<the findings file you wrote>"],
  "notes_for_next_agent": "<what the auditor of this file should look at hardest>"
}
```

`status` is `success` when the review completed and the findings file is on disk. It is not a verdict on the code — a file full of CRITICALs is still a successful review.
