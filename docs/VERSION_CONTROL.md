# Version control

T2 project is the authoritative Git repository. Track source, tests, scripts, prompts, research decisions, dependency versions, and sanitized endpoint examples. The initial commit is an interrupted-development baseline, not a claim of successful reproduction.

Raw traces, full request/response records, live ledgers, local endpoint configuration, model caches and virtual environments stay outside Git. `artifacts/provenance-index.json` records selected artifact paths and SHA-256 values; ignored evidence must remain on T2. Git is not a backup for those ignored files.

Use `codex/` branches. Each coherent implementation/research change should have a focused commit and recorded validation. Preserve failed runs and their manifests. Future runs must record the Git commit and whether their checkout was dirty, plus relevant source/config hashes. A dirty run must retain a patch snapshot in its ignored run evidence.

Do not commit credentials. The private development repository remains the authoritative working history. A separately created public repository may receive only a sanitized snapshot produced by `scripts/create_public_snapshot.py`; it starts from a new public initial commit and never receives this private Git history. The snapshot exporter selects committed tracked files only, omits untracked and ignored runtime material, replaces known deployment host/user/path fields with placeholders, and writes a manifest for review. Review that manifest and the output tree before creating a remote or pushing it. Main agent reviews changes and owns publication; coordinate writes before committing or publishing.
