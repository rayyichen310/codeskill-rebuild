# D04 runtime delta: event provenance sidecar

Keep the Fig.7 `action` and `skill` JSON fields unchanged. When and only when
you choose `{"action":"generate",...}`, add this extra sidecar:

`"evidence":{"trigger_step_ids":["..."],"response_step_ids":["..."],"outcome_step_ids":["..."],"rule_evidence":[{"rule_index":0,"step_ids":["..."]}]}`

The trigger must be a local observation or a later user clarification that
appears after the initial task request. It cannot be the initial user goal,
the task as a whole, or a report of the desired final outcome. The response
must be a later assistant action, and the outcome must be a still later tool
observation. Provide one `rule_evidence` entry for every rule, numbered from
zero in rule order. All IDs must come from the supplied full trajectory.

If the trace supports only a task-level workflow, no local observed event, or
no outcome after a response, return the paper's normal skip form. Do not force
a generated skill.

Custom delta: Figure 7 has no evidence-ID fields. D04 adds only the sidecar
needed to audit local trigger-response-outcome granularity; the original
paper fields and full-trajectory input remain intact.
