# C-only coding protocol and recovery

The campaign compares C against the preserved 12-task coding baseline. Each
of two sequential rounds starts with an empty bank, trajectory pool and
description pool. Only the same round's C trajectories can produce skills;
publication completes before the next task starts. All 12 tasks, including
hard tasks, multimodal tasks and setup failures, remain in the schedule.

The committed configuration is `configs/r015-c-only-coding.json`. Its baseline
manifest contains comparison metadata only. Public task identities and the
accepted runtime limitations are retained in the referenced evidence files.
The original experiment config stays immutable when deploying a code fix.

## Runtime settings

- Solver: DeepSeek-V4-Flash, thinking `high` / wire reasoning `max`, context
  270000, output 81920, temperature 1, top-p 0.95, provider timeout 900 seconds.
- Manager: reasoning `max`, context 270000, output 8192, safety reserve 4096,
  temperature 0, timeout 300 seconds.
- Official Harbor 0.17.1 and public OpenClaw 2026.9.3; the historical OpenClaw
  source release was 2026.7.2. This previously accepted difference is recorded
  in the campaign state. Historical container-image equality is unproven.
- Concurrency 1, no hidden call/turn cap, no automatic paid retries. Retrieval
  uses the pinned MiniLM revision and existing thresholds.

## Manager context (D03/V02)

`historical_thinking_policy` is an explicit driver setting with two accepted
values: `keep` (the backward-compatible default) and `exclude`. The latter is
an experimental manager-input view only. It removes recognized historical
thinking/reasoning blocks before token accounting or compaction while keeping
the immutable raw trace, visible assistant text, tool calls, tool results,
outcome, ordering and source identity. Unknown reasoning-like shapes remain in
the manager view and are reported, so the run cannot silently claim a broader
exclusion than the implemented schema. Manager-generated reasoning remains
`max` in both arms.

Policy identity is included in context records and every reusable derived
artifact. A keep-derived summary, candidate or reconciled historical call is
not eligible for an exclude run. A bounded replay must first run
`scripts/legacy/run_r015_thinking_ab.py prepare`, then run each arm in a distinct
directory. The fixed S07 source group is a diagnostic control only; it is not
evidence that the production pairing stage would naturally choose that group.
The harness does not run the solver or verifier, publish a bank, change
retrieval/maintenance, or resume the formal campaign.

Every trajectory-bearing request is counted with the service tokenizer and
the actual max-effort request options. The combined payload, including 2-3
task trajectories, must fit 257712 input tokens. Overflow first uses the
existing lossless projection, then action-observation summaries from the same
manager with complete step coverage and bounded original evidence. Each
summary segment is limited to eight times the output budget (65536 input
tokens with the formal profile), because a 254922-token segment exhausted
8192 output tokens in reasoning without returning a summary. The configured
context and output budget remain unchanged. Summaries
are reused within one trial; after an explicit recovery, validated summaries
can also be reused from durable journals only when the exact source steps,
request settings and original response agree. Requests include the complete
source-ID checklist. V02 requires source citations; the driver records which
complete segments were supplied and retains the final observed tool result.
The R006 stress-only limit of three tool pairs is not used in the formal run;
the complete final request must still fit the exact input budget. Invalid
outputs remain recorded; raw trajectories and source pools stay intact.
Unambiguous source-ID prefixes of at least eight characters are expanded with
an explicit mapping; unknown or ambiguous prefixes are rejected. An explicit
recovery may revalidate an existing completed response under this rule while
preserving its original rejection. Selected evidence uses the same lossless
projection to avoid reintroducing duplicate fields.
Context records retain source hashes, original/sent token counts, selected and
omitted step IDs, and summary call references. Remaining overflow stops as
context_blocked; invalid summaries stop without publishing or replaying calls.

## Trial outcomes and learning

A complete official session and verifier reward remain eligible for extraction
when the agent exits nonzero. The raw reward and exception remain attached to
the result. A validated pre-agent setup failure has no trajectory and skips
learning. Missing or contradictory artifacts stop the coordinator.

A sidecar input-budget rejection is a non-forwarded observation. It must not
be counted as a provider call, assigned a fabricated normal-call boundary, or
accepted together with evidence claiming that it was forwarded.

## Resume without replay

Ordinary completed outputs and phase files are reused only after their input,
assignment, process and artifact bindings pass validation. Uncertain or failed
processes do not trigger automatic paid retries.

For an importer failure after Harbor completed but before the trial stage was
written, use `scripts/prepare_r015_c_only_harbor_recovery.py --help` to prepare
one manifest bound to the original input, failed process, import-failure record,
Harbor result and sidecar attempts. Choose a fresh child directory under that
task and the existing execution's manager directory. Manifest preparation
makes no model calls.

Pass that manifest to the existing coordinator with `resume
--harbor-recovery-manifest <manifest>` and the original config, baseline,
state and run directory. Recovery imports the saved Harbor artifacts and uses
the same extraction/publication implementation as a normal trial. All derived
stages and output go into the recovery child directory; original input,
process, failure and raw result files remain untouched. The recovery flag is
consumed once before normal execution advances to the next task.

Existing manager calls require their separate explicit reconciliation path;
Harbor artifact recovery does not authorize replaying them. A failed or
uncertain recovery stops for inspection.

## Versioning and validation

Source, tests, the canonical config and its required metadata are committed
in Git. Generated archives, duplicate staging trees and raw runtime logs stay
outside the source commit. They are preserved locally, not deleted.

Run `PYTHONPATH=src python -m unittest discover -s tests -q`. The recovery
integration test uses the real coordinator, importer, manager client, journals,
phase writes and publication against a controlled HTTP service. It covers a
zero-reward agent exception, a non-forwarded terminal record, unchanged
original artifacts, next-task advancement, and rejection of a repeated recovery.
This controlled check is distinct from importing the real retained trial and
from resuming the formal campaign.
