Choose whether the anchor SOP candidate and earlier ranked SOP candidates share
a substantive reusable multi-step method. Select one group of two or three
different tasks, including the anchor, or return no_related_group.

Use the supplied SOPs, applicability, constraints, observed results, limitations,
and official outcomes. No original trajectories are provided. A shared method
may be a subprocedure of otherwise different tasks. Similar task names,
languages, tools, or generic read/edit/test steps alone do not justify a group.
Do not force unrelated lessons together. Local successes may be useful despite
whole-task failure, but do not reinterpret a failed official outcome as success.

The next stage synthesizes only these selected SOPs and their context. It may
retain useful conditional steps supported by one SOP; every rule need not occur
in every task. Choose a group with a coherent shared method, not merely enough
candidates to fill a quota.

Return exactly one JSON object:
{"action":"select","selected_instance_ids":["anchor task ID","other task ID"],"shared_subprocedure":"shared multi-step method","instance_evidence":[{"canonical_instance_id":"anchor task ID","description_evidence":"which SOP steps support the shared method"},{"canonical_instance_id":"other task ID","description_evidence":"which SOP steps support the shared method"}],"reason":"why these SOPs belong together"}
Include one instance_evidence entry per selected task. Copy complete supplied
task IDs exactly. Or return:
{"action":"no_related_group","reason":"why no substantive shared method is supported"}.