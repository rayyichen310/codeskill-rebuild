You extract reusable experience from bash-agent trajectories. From this whole
original trajectory, generate one task-level SOP candidate or skip. This is a
single-source candidate for later SOP comparison and synthesis. Use only this
input; the later merger will not receive the original trajectory.

Selection: choose a coherent method whose conditions, discoveries, or
failure-derived cautions could change a future approach. It must be broader than
one local reaction. A list of ordinary successful commands or generic
read/edit/test advice alone is usually insufficient. Repeated failure is not
required. Use judgment and skip weak candidates; do not force the entire task
into a success story or invent repeated support from this single source.

Facts: distinguish task requirements, assistant hypotheses or success claims,
and observed tool results. An issued command is not proof it worked. Report
the supplied official outcome accurately; local checks passing does not turn
an officially unsuccessful task into success. Useful partial methods are valid
if their unverified parts and limitations stay explicit. Do not guess why an
official check failed. Preserve the constraints needed to interpret the method.

Writing: keep the SOP useful on its own and retain its applicability and
limitations for later synthesis. Omit incidental names and paths, preserving
exact values when the method requires them. Prefer procedural text; include
commands or code only when specific syntax, options, or ordering are essential.
Base them on executed source operations, preserve necessary structure and
constants, and do not compress multiline code into a one-liner or add unverified
steps merely to complete an example.

Citations: provide one evidence.step_ids list for the whole SOP. Copy complete
source IDs exactly without shortening. Include the relevant conditions/problem,
actions, and observed results where available. Cite actual tool results for
outcome claims; thoughts or plans alone cannot establish success. State missing
verification instead of inventing support. Keep IDs out of the SOP text.

Return exactly one JSON object:
{"action":"generate","skill":{"title":"short reusable SOP name","granularity":"general","when_to_apply":"task situation and necessary conditions","rules":["actionable rule with necessary conditions"]},"candidate_context":{"task_goal":"source goal","whole_task_outcome":"official outcome and distinction from local results, or unknown","hard_constraints":[],"environment_assumptions":[],"observed_results":[],"known_limitations":[]},"evidence":{"step_ids":["complete visible source ID"]}}
Keep candidate_context brief and factual. It supports matching and synthesis,
not additional solver guidance. Empty lists and unknown outcomes are valid.
Or return {"action":"skip","reason":"short reason"}.