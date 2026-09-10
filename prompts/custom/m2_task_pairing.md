# D02 custom prompt: choose an evidence-supported task group

The user provides one anchor trajectory description and the MiniLM-ranked
candidate descriptions. Decide whether the anchor and exactly one group of
two or three different instances share a reusable multi-step procedure. The
total group size includes the anchor. The shared procedure may be a
subprocedure within otherwise different tasks; the complete solution and
domain do not need to match. Do not select based only on language, repository,
task names, error keywords, or generic read/edit/test activity. Do not force
a group when the descriptions do not establish a substantive shared sequence.

Return exactly one JSON object:

- `{"action":"select","selected_instance_ids":["anchor", "candidate"],"shared_subprocedure":"the observed multi-step subprocedure","instance_evidence":[{"canonical_instance_id":"anchor","description_evidence":"which described steps support it"},{"canonical_instance_id":"candidate","description_evidence":"which described steps support it"}],"reason":"evidence-supported shared procedure"}` for a group of two or three that includes the anchor; or
- `{"action":"no_related_group","reason":"why the descriptions do not support a shared multi-step procedure"}`.

The subsequent extractor will receive full normalized trajectories, never
only these descriptions.

Custom delta: the paper specifies related trajectories as input but does not
fully prescribe how to pair them. D02 uses MiniLM only to rank up to 12
candidates and DeepSeek to make the final no-force selection. This R011
calibration preserves the original R009 prompt/results separately and is used
once for the fixed ten-source pool.
