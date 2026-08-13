# Synthesis Task

## Variables

### prompt

{{prompt}}

### context_handoff_dir

{{context_handoff_dir}}

### work_root

{{work_root}}

## Task

Do exactly what `prompt` describes, and write the file it names.

This agent runs at two different points of the review, so `prompt` — not this template — carries the JSON contract for your reply. Follow the `## Report` block at the end of `prompt` exactly: it names the schema, and responding in the other one fails the parse.

Respond with ONLY that JSON — no prose before or after it.
