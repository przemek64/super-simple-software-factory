# Spec Axis Task

## Variables

### prompt

{{prompt}}

### context_handoff_dir

{{context_handoff_dir}}

### work_root

{{work_root}}

## Task

Do exactly what `prompt` describes: read the spec sources it names, judge the named diff against them, write the prose report to the path it names, and return your findings in the envelope.

Follow the `## Report` block at the end of `prompt` exactly — it carries the JSON contract.

Respond with ONLY that JSON, no prose before or after it.
