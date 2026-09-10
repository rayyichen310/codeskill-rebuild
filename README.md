# CODESKILL rebuild

This is a from-scratch research implementation of CODESKILL. It contains traceable skill banks, event extraction, a frozen trial lifecycle, and a sidecar that works with the public OpenClaw plugin. It is not a completed Terminal-Bench results report.

## Implemented and offline-tested

- Task and event-skill extraction, retrieval, provenance, bank freezing, and staged maintenance.
- OpenClaw sidecar full-payload preflight, durable event overlay, SQLite/JSONL native-compaction detectors, and the public plugin's native-summary permit.
- The R012 event runner sends the complete raw trace unchanged when it fits the exact context allowance. It uses R006/D03 evidence compaction only when the trace exceeds that allowance. That path retains the full raw trace, covers every source step, forwards only auditable original fragments, and explicitly stops if the final request still does not fit.
- Explicit R014 unlimited development-ledger activation that cannot be silently reduced by an older finite profile.

Python **3.12 or newer** is required.

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
python -m unittest discover -s tests -v
```

The complete offline suite currently reports **144 / 144 passed**. It uses a local fake manager and isolated HTTP/SQLite fixtures to verify program behavior and failure preservation. It does not establish live manager, Harbor, official solver/verifier, or Terminal-Bench results.

## Not yet established

- The Harbor adapter, official solver/verifier, and formal A/B trials have not received live validation.
- There is no claim that long-trace compaction improves success rate, cost, or token use.
- Complete live-run artifacts, raw traces, requests/responses, mutable ledgers, model caches, and credentials are absent from the repository.

Read the [reproduction contract](docs/REPRODUCTION_SPEC.md), [implementation status](docs/IMPLEMENTATION_STATUS.md), and [research status](docs/STATUS.md) first. `configs/model-endpoints.example.json` is a safe example. Copy it to the locally ignored `configs/model-endpoints.json`, then fill in your own service URL and model ID. Do not commit credentials or a live endpoint configuration.

`scripts/create_public_snapshot.py` creates a public snapshot from tracked files in a specified commit. It does not copy Git history or untracked runtime files, and replaces known deployment hosts, user identifiers, and paths with portable placeholders. See the [version-control policy](docs/VERSION_CONTROL.md).
