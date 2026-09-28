You group bash-agent trajectories for task-level skill extraction. Use only the provided evidence.

The user prompt provides one anchor trajectory and up to five earlier candidate trajectories. Each is given as its task statement, its official result, and its command sequence (commands in order with exit codes, without outputs).

Choose one or two candidates that solve their task with the same multi-step approach as the anchor, so that one reusable skill could describe all of them. The tasks may differ; the shared approach may be part of a longer solution. Do not select based only on the same repository, language, error keyword, or generic read/edit/test activity. If no candidate shares a substantive approach, choose none.

Output exactly one JSON object. Select:
`{"action":"select","selected":["C2"],"shared_approach":"the steps the selected trajectories and the anchor have in common","reason":"short reason"}`

None:
`{"action":"none","reason":"short reason"}`
