You are preparing bounded context for a Task description, not extracting a skill.

Summarize the supplied source segment for the whole task. Account for every
required step ID in covered_step_ids, in source order. Cite a small set of
original step IDs in verbatim_evidence_step_ids that lets a later description
inspect the task instruction, an actual operation and observation, and the
observed outcome. Keep the final tool result when this is the final segment.

Use only supplied steps. Do not infer an unseen result or claim that a local
operation succeeded because the whole task succeeded. Omitted source steps
remain covered by the summary, but only cited original steps are forwarded
verbatim. Return JSON with summary, covered_step_ids, and
verbatim_evidence_step_ids.
