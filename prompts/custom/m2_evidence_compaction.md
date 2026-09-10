# V02 custom prompt: summarize one action-observation evidence segment

Summarize only the supplied contiguous action-observation segment. Preserve
what the agent tried, important observations or errors, modifications, and
verification results when observed. Do not infer hidden tests, root causes, or
success. Return exactly:

`{"summary":"concise observed evidence","evidence_step_ids":["source-entry-id", "..."]}`

Every claim in `summary` must cite one or more source entry IDs in
`evidence_step_ids`; use only IDs in this segment. The later extractor will
receive these summaries together with the cited original entries.

Custom delta: V02 is used only when the exact server chat-template count shows
that a full trajectory cannot fit the deliberately configured input budget. It
does not replace normal full-trajectory extraction.
