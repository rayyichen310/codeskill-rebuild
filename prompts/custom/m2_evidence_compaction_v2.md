# R006/V02 custom prompt: coverage summary with a bounded verbatim subset

Summarize one contiguous action-observation segment. The source archive stays
available outside this call; account for all procedures, observations, changes,
and observed results in your summary without inventing causes, hidden tests, or
success.

Return exactly:

`{"summary":"concise observed coverage","covered_step_ids":["every segment ID once"],"verbatim_evidence_step_ids":["IDs for at most three complete action-observation pairs"]}`

`covered_step_ids` must list every supplied segment source ID exactly once. It
records coverage only: its raw text will not all be replayed. Select no more
than three complete action-observation pairs in `verbatim_evidence_step_ids`.
Choose pairs that preserve the most important precondition, response, outcome,
and, in the final segment, the original final observed result. The later
extractor receives only your summary plus the selected original pairs, so skip
unsupported details rather than citing omitted raw text.

Custom delta: R006 refines V02 after the first stress run showed that expanding
every coverage citation recreated the over-budget trajectory. This is only for
the declared budget-stress variant and does not replace normal full-trajectory
extraction.
