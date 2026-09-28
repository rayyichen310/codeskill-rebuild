Synthesize one reusable task-level skill from two or three selected single-task
SOP candidates, or skip. Use only the supplied SOPs and their context. You do
not have their original trajectories and must not claim to have checked them.

Find the coherent shared method. Keep the conditions that make each step valid;
useful conditional steps may come from one SOP and do not need support from
every task. Do not force unrelated methods together, remove conditions to make
rules look universal, or turn a case-specific choice into a general default.
Skip when only generic operations or unrelated local reactions remain.

Preserve important constraints, uncertainty, and limitations. Distinguish local
observed results from whole-task outcomes, respecting the supplied official
outcomes when a candidate's own success claim conflicts. Do not invent missing
steps, verification results, or explanations of failures.

Write a self-contained skill with actionable rules. Prefer procedural text;
retain commands/code only when their syntax, options, or ordering are essential
and already supported by the supplied SOP. Preserve necessary structure and
fixed values; do not invent executable examples or compress multiline code.
Deduplicate repeated guidance without losing distinct conditions.

Return exactly one JSON object:
{"action":"generate","skill":{"title":"short reusable method name","granularity":"general","when_to_apply":"shared situation and necessary conditions","rules":["actionable rule with necessary conditions"]},"evidence":{"source_candidate_ids":["complete source SOP candidate ID","another complete source SOP candidate ID"]}}

Cite the SOP candidates actually used, by copying complete candidate_id values
exactly. This is one citation set for the whole skill, not per-rule citations
to every task. The program retains the SOP-to-original-step links separately
for later inspection. Do not output raw step IDs, trace paths, or source IDs
inside solver-facing skill text.
Or return {"action":"skip","reason":"short reason"}.