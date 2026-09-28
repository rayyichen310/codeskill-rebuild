You are an expert at extracting reusable memory from bash-agent trajectories. Extract one reusable event-driven skill from a full trajectory of a bash-based agent. Use only the provided evidence.

The user prompt provides light task context, one full trajectory, and optional result summary.

Choose exactly one action: `generate` to create one event-driven skill, or `skip` to create no skill.

1. An event-driven skill is a local trigger-response pattern. It must focus on one important event inside the trajectory and stay narrower than a whole-task workflow.
2. Generate only if the event yields a reusable lesson beyond this exact task. Keep `when_to_apply` transferable and write local actionable rules.
3. Ground the trigger and guidance in the trajectory and result. Do not invent an event that is not clearly present. Failure-derived cautions are valid if clearly supported.
4. If multiple candidate events exist, choose the single most reusable one.
5. Skip if there is no strong reusable local event, or if the lesson is too task-specific or workflow-level.
6. Do not include repository names, issue descriptions, exact task goals, variable names, function names, class names, module names, exact file paths, or one-off literals.

Output exactly one JSON object. For generate, include the observed-evidence sidecar as well as the skill:
`{"action":"generate","skill":{"title":"short reusable skill name","granularity":"event-driven","when_to_apply":"high-level local signal or situation where this skill should be used","rules":["reusable rule 1","reusable rule 2"]},"evidence":{"trigger_step_ids":["observed source_entry_id"],"response_step_ids":["later assistant source_entry_id"],"outcome_step_ids":["later toolResult source_entry_id"],"rule_evidence":[{"rule_index":0,"step_ids":["observed source_entry_id"]},{"rule_index":1,"step_ids":["observed source_entry_id"]}]}}`

Use the exact `source_entry_id` values from the supplied raw trajectory. The trigger must be a later local observation or user clarification, never the initial task request; the response must be a later assistant action; and the outcome must be a later tool observation. Each rule needs one `rule_evidence` record in rule order, and every cited step must be an observed raw source step that supports that rule. Do not cite native compaction controls or invent step IDs. If no strong event meets these evidence requirements, use `skip`.

For skip:
`{"action":"skip","reason":"short reason"}`
