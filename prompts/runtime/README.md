# Runtime deltas

Runtime calls use the Figure 6–9 transcriptions as their system prompts. The
only harness delta is that evidence is a normalized OpenClaw trajectory rather
than a bash-only trajectory. `benchmark`, source ancestry, step IDs, call
budget, and bank transaction data remain sidecar metadata and are not inserted
into solver-facing skills. Paper `general` and `event-driven` map after parsing
to internal `task` and `event` records; this adapter is recorded in
`pipeline.paper_skill_to_internal`.
