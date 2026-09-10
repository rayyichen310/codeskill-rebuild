# D01 custom prompt: evidence-grounded short trajectory description

Summarize only observed coding-agent evidence. Do not infer hidden verifier
tests, unobserved causes, or task success beyond the supplied outcome. Return
exactly one JSON object with all of these fields:

`{"task_family":"...","observed_obstacle":"...","attempted_procedure":"...","observed_outcome":"...","source_step_ids":["..."]}`

Every stated claim must be supported by one or more listed `source_step_ids`.
Use only IDs present in the supplied trajectory. Keep an unknown cause as
unknown. The description is an index for pairing; it is not a substitute for
the full trajectory during extraction.

Custom delta: D01 supplies a compact, step-cited retrieval record before the
paper's multi-trajectory extraction prompt. It does not add skills or rewrite
the source trajectory.

`task_family` must be a reusable activity label supported by the observed
procedure, such as a class of diagnosis, migration, repair, or validation. Do
not merely repeat an instance ID, benchmark task name, repository name, or the
user's one-off request.
