# R011 custom prompt: repair event provenance only

You receive one original full normalized trajectory, the model's prior JSON
output, and the exact validator error. Do not improve, generalize, rewrite, or
otherwise change the `skill` object. Re-evaluate only the evidence sidecar
against the original trajectory.

If valid local evidence exists, return the same top-level `action:"generate"`
and the exactly unchanged `skill` object from `original_model_output`, with a
corrected `evidence` sidecar. The sidecar must include nonempty
`trigger_step_ids`, `response_step_ids`, `outcome_step_ids`, and one ordered
`rule_evidence` entry for every rule. The trigger must be a local observed
tool result or later user clarification after the initial task request; an
assistant response must occur after it; and a later tool result must provide
the outcome.

If the prior skill cannot be supported without changing it, return exactly
`{"action":"cannot_repair_evidence","reason":"why the unchanged skill lacks valid local evidence"}`.

Do not invent steps, commands, results, or source IDs. This is a one-shot
provenance repair, not a new extraction.
