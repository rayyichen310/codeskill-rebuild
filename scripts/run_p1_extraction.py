"""P1 lean extraction runner (docs/DECISIONS.md §3, "P1 做法選 B").

Builds, for each source run (SWE codeskill, TB2 round 1, TB2 round 2), two banks
from the same trajectories in task order:

  extract-only  every candidate that passes lint (after at most one revision);
  maintained    the same candidates passed through Fig. 9 one by one.

Stage 1 (per trajectory, parallel): Fig. 7 up to three times, then pairing
with earlier trajectories of the same run and Fig. 6 on the selected group.
Stage 2 (per run, sequential): Fig. 9 against the maintained bank.

Every DeepSeek call goes through ManagerClient (ledger, exact-token preflight,
request/response files).  Parsed results are cached by request hash in
calls.jsonl, so a rerun after a failure repeats no successful call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from codeskill_rebuild.context import ContextBlocked
from codeskill_rebuild.compact_trace import command_sequence, official_result, render, step_count, task_statement
from codeskill_rebuild.manager import ManagerCallError, ManagerClient, ManagerProfile, ServerMessageTokenCounter
from codeskill_rebuild.retrieval import MiniLMEncoder, cosine
from codeskill_rebuild.retrieval_query import task_query_fields
from codeskill_rebuild.skill_lint import lint_skill, repo_terms_for
from codeskill_rebuild.types import utc_now, write_json

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home() / "ray"
SWE = HOME / "tmp/codeskill-swe-pilot-20260928"
TB2 = HOME / "tmp/r015-c-only-formal-20260914-01"
MINILM_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
PROMPTS = {
    "fig06": ROOT / "prompts/custom/p1_fig06_task_extraction.md",
    "fig07": ROOT / "prompts/custom/p1_fig07_event_extraction.md",
    "pairing": ROOT / "prompts/custom/p1_task_pairing.md",
    "templates": ROOT / "prompts/custom/p1_user_templates.md",
    "fig09": ROOT / "prompts/paper/fig09_maintenance.md",
}
EVENT_ATTEMPTS = 3
PAIRING_CANDIDATES = 5
MAINTENANCE_RETRIEVED = 5
PAPER_GRANULARITY = {"event": "event-driven", "task": "general"}


class OutputError(ValueError):
    """The model returned JSON that does not match the prompt's schema."""


def templates() -> dict[str, str]:
    sections = PROMPTS["templates"].read_text(encoding="utf-8").split("## ")[1:]
    return {name: body.strip("\n") for name, _, body in (s.partition("\n") for s in sections)}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sources() -> list[dict[str, Any]]:
    swe_order = json.loads((SWE / "pilot-state.json").read_text())["task_order"]
    runs = [{
        "run": "swe-codeskill", "benchmark": "swe-bench",
        "tasks": [(t, SWE / "trials/codeskill" / f"{i:02d}-{t}" / "trajectory-live.json") for i, t in enumerate(swe_order, 1)],
    }]
    tb2 = json.loads((TB2 / "state.json").read_text())
    for round_id in ("1", "2"):
        base = TB2 / "attempts/attempt-003" / f"round-{round_id}"
        tasks = [(t, base / t / "official-harbor/trajectory-live.json") for t in tb2["rounds"][round_id]["task_order"]]
        runs.append({"run": f"tb2-r{round_id}", "benchmark": "terminal-bench", "tasks": tasks})
    return runs


# ---------------------------------------------------------------- model calls


class Caller:
    """One ManagerClient per work unit, with a request-hash cache for reruns."""

    def __init__(self, directory: Path, profile: ManagerProfile, contract: dict[str, str]) -> None:
        self.client = ManagerClient(profile, directory, contract, directory / "ledger.json", ServerMessageTokenCounter(profile.base_url))
        self.cache_path = directory / "calls.jsonl"
        self.cache = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text().splitlines():
                record = json.loads(line)
                self.cache[record["key"]] = record

    def __call__(self, purpose: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        key = sha256_text(json.dumps(messages, sort_keys=True, ensure_ascii=False))
        if key in self.cache:
            return self.cache[key]
        result = self.client.call_json(purpose=purpose, messages=messages, call_metadata={"request_key": key})
        record = {
            "key": key, "purpose": purpose, "call_id": result["call_id"], "json": result["json"],
            "content": result["response"]["choices"][0]["message"]["content"],
            "usage": result["response"].get("usage"),
        }
        with self.cache_path.open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.cache[key] = record
        return record


def parse_skill(value: dict[str, Any], granularity: str) -> dict[str, Any] | None:
    """Return the generated skill, or None for skip."""
    if value.get("action") == "skip":
        return None
    skill = value.get("skill")
    if value.get("action") != "generate" or not isinstance(skill, dict):
        raise OutputError(f"expected generate or skip, got {str(value)[:200]}")
    return _skill_fields(skill, granularity)


def _skill_fields(skill: dict[str, Any], granularity: str) -> dict[str, Any]:
    if skill.get("granularity") != PAPER_GRANULARITY[granularity]:
        raise OutputError(f"granularity must be {PAPER_GRANULARITY[granularity]}")
    for name in ("title", "when_to_apply"):
        if not isinstance(skill.get(name), str) or not skill[name].strip():
            raise OutputError(f"skill needs a nonempty {name}")
    rules = skill.get("rules")
    if not isinstance(rules, list) or not rules or not all(isinstance(r, str) and r.strip() for r in rules):
        raise OutputError("skill needs a nonempty list of rules")
    return {"title": skill["title"], "granularity": granularity, "when_to_apply": skill["when_to_apply"], "rules": rules}


def paper_view(skill: dict[str, Any], with_id: bool = False) -> dict[str, Any]:
    view = {"title": skill["title"], "granularity": PAPER_GRANULARITY[skill["granularity"]],
            "when_to_apply": skill["when_to_apply"], "rules": skill["rules"]}
    return {"skill_id": skill["skill_id"], **view} if with_id else view


def lint_and_revise(call: Caller, purpose: str, messages: list[dict[str, str]], first: dict[str, Any],
                    skill: dict[str, Any], parse: Any, terms: frozenset[str], tpl: dict[str, str]) -> tuple[Any, dict[str, Any]]:
    """One revision call when lint finds identifiers; still failing means drop."""
    findings = lint_skill(skill, terms)
    record: dict[str, Any] = {"findings": findings}
    if not findings:
        return first["json"], record
    listed = "\n".join(f"- {f['field']}: {f['kind']} `{f['text']}`" for f in findings)
    revision_messages = messages + [
        {"role": "assistant", "content": first["content"]},
        {"role": "user", "content": tpl["lint_revision"].format(findings=listed)},
    ]
    revised = call(purpose + "-lint-revision", revision_messages)
    record["revision_call"] = revised["call_id"]
    try:
        revised_skill = parse(revised["json"])
    except OutputError as error:
        record.update({"outcome": "dropped", "reason": f"invalid revision: {error}"})
        return None, record
    if revised_skill is None:
        record.update({"outcome": "dropped", "reason": "revision chose skip or drop"})
        return None, record
    record["revision_findings"] = lint_skill(revised_skill, terms)
    if record["revision_findings"]:
        record.update({"outcome": "dropped", "reason": "identifiers remain after revision"})
        return None, record
    record["outcome"] = "revised"
    return revised["json"], record


# ---------------------------------------------------------------- stage 1: extraction


def extract_unit(unit: dict[str, Any], run: dict[str, Any], earlier: list[dict[str, Any]], out: Path,
                 profile: ManagerProfile, contract: dict[str, str], tpl: dict[str, str], terms: frozenset[str]) -> dict[str, Any]:
    directory = out / run["run"] / unit["task"]
    directory.mkdir(parents=True, exist_ok=True)
    call = Caller(directory, profile, contract)
    trajectory = unit["trajectory"]
    trace = render(trajectory)
    record: dict[str, Any] = {"task": unit["task"], "events": [], "task_skill": None, "candidates": []}
    try:
        fig07 = PROMPTS["fig07"].read_text(encoding="utf-8")
        previous: list[dict[str, Any]] = []
        for attempt in range(1, EVENT_ATTEMPTS + 1):
            if previous:
                user = tpl["event_repeat"].format(benchmark=run["benchmark"], trace=trace,
                                                  previous=json.dumps([paper_view(s) for s in previous], ensure_ascii=False, indent=1))
            else:
                user = tpl["event"].format(benchmark=run["benchmark"], trace=trace)
            messages = [{"role": "system", "content": fig07}, {"role": "user", "content": user}]
            purpose = f"{run['run']}:{unit['task']}:event-{attempt}"
            first = call(purpose, messages)
            attempt_record: dict[str, Any] = {"attempt": attempt, "call": first["call_id"]}
            record["events"].append(attempt_record)
            try:
                skill = parse_skill(first["json"], "event")
            except OutputError as error:
                attempt_record["outcome"] = f"invalid_output: {error}"
                break
            if skill is None:
                attempt_record["outcome"] = "skip"
                break
            previous.append(skill)
            final, attempt_record["lint"] = lint_and_revise(call, purpose, messages, first, skill,
                                                           lambda v: parse_skill(v, "event"), terms, tpl)
            if final is None:
                attempt_record["outcome"] = "dropped_by_lint"
                continue
            steps = final.get("evidence_steps")
            valid = isinstance(steps, list) and bool(steps) and all(isinstance(n, int) and 1 <= n <= step_count(trajectory) for n in steps)
            attempt_record["outcome"] = "candidate"
            record["candidates"].append({
                **parse_skill(final, "event"), "source_instance_ids": [unit["task"]],
                "evidence_steps": steps, "evidence_valid": valid, "extraction": purpose,
            })
        if earlier:
            record["task_skill"] = extract_task(call, unit, run, earlier, tpl, terms, record)
    except (ManagerCallError, ContextBlocked) as error:  # service or tokenizer down: rerun resumes from calls.jsonl
        record["failed"] = str(error)[:500]
    write_json(directory / "unit.json", record)
    return record


def extract_task(call: Caller, unit: dict[str, Any], run: dict[str, Any], earlier: list[dict[str, Any]],
                 tpl: dict[str, str], terms: frozenset[str], record: dict[str, Any]) -> dict[str, Any]:
    ranked = sorted(earlier, key=lambda e: -cosine(unit["vector"], e["vector"]))[:PAIRING_CANDIDATES]

    def brief(u: dict[str, Any]) -> str:
        t = u["trajectory"]
        return f"TASK:\n{task_statement(t)}\nOFFICIAL RESULT: {official_result(t)}\nCOMMANDS:\n{command_sequence(t)}"

    labels = {f"C{i}": u for i, u in enumerate(ranked, 1)}
    user = tpl["pairing"].format(anchor=brief(unit), candidates="\n\n".join(f"{label}\n{brief(u)}" for label, u in labels.items()))
    purpose = f"{run['run']}:{unit['task']}:pairing"
    pairing = call(purpose, [{"role": "system", "content": PROMPTS["pairing"].read_text(encoding="utf-8")}, {"role": "user", "content": user}])
    result: dict[str, Any] = {"pairing_call": pairing["call_id"], "ranked": [u["task"] for u in ranked], "pairing": pairing["json"]}
    selected = pairing["json"].get("selected") if pairing["json"].get("action") == "select" else None
    if pairing["json"].get("action") == "none":
        result["outcome"] = "no_pair"
        return result
    if not isinstance(selected, list) or not 1 <= len(selected) <= 2 or not set(selected) <= set(labels) or len(set(selected)) != len(selected):
        result["outcome"] = "invalid_pairing_output"
        return result
    group = [unit] + [labels[label] for label in selected]
    trajectories = "\n\n".join(f"TRAJECTORY T{i}\n{render(u['trajectory'])}" for i, u in enumerate(group, 1))
    messages = [{"role": "system", "content": PROMPTS["fig06"].read_text(encoding="utf-8")},
                {"role": "user", "content": tpl["task"].format(benchmark=run["benchmark"], trajectories=trajectories)}]
    purpose = f"{run['run']}:{unit['task']}:task"
    first = call(purpose, messages)
    result.update({"group": [u["task"] for u in group], "task_call": first["call_id"]})
    try:
        skill = parse_skill(first["json"], "task")
    except OutputError as error:
        result["outcome"] = f"invalid_output: {error}"
        return result
    if skill is None:
        result["outcome"] = "skip"
        return result
    final, result["lint"] = lint_and_revise(call, purpose, messages, first, skill, lambda v: parse_skill(v, "task"), terms, tpl)
    if final is None:
        result["outcome"] = "dropped_by_lint"
        return result
    steps = final.get("evidence_steps")
    valid = isinstance(steps, dict) and set(steps) == {f"T{i}" for i in range(1, len(group) + 1)} and all(
        isinstance(v, list) and v and all(isinstance(n, int) and 1 <= n <= step_count(group[int(k[1:]) - 1]["trajectory"]) for n in v)
        for k, v in steps.items())
    result["outcome"] = "candidate"
    record["candidates"].append({
        **parse_skill(final, "task"), "source_instance_ids": [u["task"] for u in group],
        "evidence_steps": steps, "evidence_valid": valid, "extraction": purpose,
    })
    return result


# ---------------------------------------------------------------- stage 2: banks


def bank_skill(candidate: dict[str, Any], run: dict[str, Any], sequence: int, parents: list[str], version: int = 1) -> dict[str, Any]:
    identity = json.dumps([run["run"], candidate["title"], candidate["when_to_apply"], candidate["rules"], parents])
    return {
        "skill_id": "p1-" + sha256_text(identity)[:16], "title": candidate["title"], "granularity": candidate["granularity"],
        "when_to_apply": candidate["when_to_apply"], "rules": candidate["rules"], "benchmark": run["benchmark"],
        "provenance": {"source_instance_ids": candidate["source_instance_ids"], "parent_skill_ids": parents,
                       "evidence_steps": candidate.get("evidence_steps"), "evidence_valid": candidate.get("evidence_valid"),
                       "extraction": candidate.get("extraction")},
        "version": version, "status": "active", "created_sequence": sequence, "origin_run": run["run"],
    }


def maintain(run: dict[str, Any], candidates: list[dict[str, Any]], out: Path, profile: ManagerProfile, contract: dict[str, str],
             tpl: dict[str, str], terms: frozenset[str], encoder: MiniLMEncoder, lock: threading.Lock) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    directory = out / run["run"] / "maintenance"
    directory.mkdir(parents=True, exist_ok=True)
    call = Caller(directory, profile, contract)
    fig09 = PROMPTS["fig09"].read_text(encoding="utf-8")
    bank: list[dict[str, Any]] = []
    vectors: dict[str, list[float]] = {}
    log = []

    def vector(skill: dict[str, Any]) -> list[float]:
        with lock:
            return encoder.index_skill(skill)[0]

    for index, candidate in enumerate(candidates):
        pool = [s for s in bank if s["status"] == "active" and s["granularity"] == candidate["granularity"]]
        query = vector(candidate)
        retrieved = sorted(pool, key=lambda s: (-cosine(query, vectors[s["skill_id"]]), s["skill_id"]))[:MAINTENANCE_RETRIEVED]
        user = tpl["maintenance"].format(
            candidate=json.dumps(paper_view(candidate), ensure_ascii=False, indent=1),
            retrieved=json.dumps([paper_view(s, with_id=True) for s in retrieved], ensure_ascii=False, indent=1))
        messages = [{"role": "system", "content": fig09}, {"role": "user", "content": user}]
        purpose = f"{run['run']}:maintenance-{index + 1:03d}"
        entry: dict[str, Any] = {"candidate": candidate["extraction"], "retrieved": [s["skill_id"] for s in retrieved]}
        log.append(entry)
        first = call(purpose, messages)
        entry["call"] = first["call_id"]
        value = first["json"]
        action = value.get("action")
        entry["action"] = action
        if action == "drop":
            continue
        if action == "add":
            skill = bank_skill(candidate, run, len(bank) + 1, [])
            bank.append(skill)
            vectors[skill["skill_id"]] = vector(skill)
            continue
        target = next((s for s in retrieved if s["skill_id"] == value.get("merge_target_skill_id")), None)
        if action != "merge" or target is None:
            entry["action"] = f"invalid_output: {str(value)[:200]}"
            continue

        def parse_merge(v: dict[str, Any]) -> dict[str, Any] | None:
            if v.get("action") == "drop":
                return None
            if v.get("action") != "merge" or v.get("merge_target_skill_id") != target["skill_id"] or not isinstance(v.get("skill"), dict):
                raise OutputError("revision must keep the same merge target")
            return _skill_fields(v["skill"], target["granularity"])

        try:
            merged = parse_merge(value)
        except OutputError as error:
            entry["action"] = f"invalid_output: {error}"
            continue
        final, entry["lint"] = lint_and_revise(call, purpose, messages, first, merged, parse_merge, terms, tpl)
        if final is None:
            entry["action"] = "merge_dropped_by_lint"
            continue
        merged = parse_merge(final)
        merged["source_instance_ids"] = sorted(set(target["provenance"]["source_instance_ids"]) | set(candidate["source_instance_ids"]))
        merged["extraction"] = candidate["extraction"]
        target["status"] = "superseded"
        skill = bank_skill(merged, run, len(bank) + 1, [target["skill_id"]], target["version"] + 1)
        bank.append(skill)
        vectors[skill["skill_id"]] = vector(skill)
        entry["merged_into"] = skill["skill_id"]
    write_json(directory / "log.json", log)
    return bank, log


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--endpoints", type=Path, required=True, help="model-endpoints.json (not tracked)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--plan-only", action="store_true", help="load sources and render prompts; no model calls")
    parser.add_argument("--runs", nargs="+", choices=("swe-codeskill", "tb2-r1", "tb2-r2"), help="default: all")
    args = parser.parse_args()

    service = json.loads(args.endpoints.read_text())["services"]["deepseek_flash"]
    profile = ManagerProfile(base_url=service["base_url"], model=service["model_id"], timeout_seconds=300, max_output_tokens=65536,
                             manager_context_tokens=524288, safety_tokens=4096, temperature=0.0, reasoning_effort="max",
                             max_total_calls=None, output_budget_profile="swe_pilot_64k")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    if dirty and not args.plan_only:
        sys.exit("refusing a live run from a dirty tree; commit first")
    prompt_hashes = {name: sha256_text(path.read_text(encoding="utf-8")) for name, path in PROMPTS.items()}
    contract = {"plan": "docs/DECISIONS.md#p1-option-b", "git_commit": commit}
    tpl = templates()

    encoder = MiniLMEncoder(revision=MINILM_REVISION)
    encoder_meta = encoder.load()
    runs = [r for r in sources() if args.runs is None or r["run"] in args.runs]
    missing = []
    for run in runs:
        units = []
        for task, path in run["tasks"]:
            if not path.is_file():
                missing.append({"run": run["run"], "task": task, "path": str(path)})
                continue
            trajectory = json.loads(path.read_text())
            first_user = "".join(x.get("text", "") for x in trajectory["steps"][0]["content"] if isinstance(x, dict) and x.get("type") == "text")
            vector, _ = encoder.encode_query("task", task_query_fields(first_user, task))
            units.append({"task": task, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "trajectory": trajectory, "vector": vector})
        run["units"] = units
        run["terms"] = repo_terms_for([u["task"] for u in units])
    manifest: dict[str, Any] = {
        "kind": "p1_extraction", "started_at_utc": utc_now(), "git_commit": commit, "git_dirty": bool(dirty),
        "prompts": {name: {"path": str(path.relative_to(ROOT)), "sha256": prompt_hashes[name]} for name, path in PROMPTS.items()},
        "profile": {k: getattr(profile, k) for k in ("model", "max_output_tokens", "temperature", "reasoning_effort", "timeout_seconds")},
        "encoder": encoder_meta, "event_attempts": EVENT_ATTEMPTS, "pairing_candidates": PAIRING_CANDIDATES,
        "maintenance_retrieved": MAINTENANCE_RETRIEVED, "missing_sources": missing,
        "sources": {run["run"]: [{k: u[k] for k in ("task", "path", "sha256")} for u in run["units"]] for run in runs},
    }
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / f"manifest-{'-'.join(r['run'] for r in runs)}.json"
    write_json(manifest_path, manifest)
    print(json.dumps({r["run"]: len(r["units"]) for r in runs}), "missing:", [m["task"] for m in missing])
    if args.plan_only:
        return

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {(run["run"], u["task"]): pool.submit(extract_unit, u, run, run["units"][:i], args.out, profile, contract, tpl, run["terms"])
                   for run in runs for i, u in enumerate(run["units"])}
        units = {key: future.result() for key, future in futures.items()}
    failed = [k for k, v in units.items() if "failed" in v]
    if failed:
        sys.exit(f"extraction calls failed for {failed}; rerun to resume from calls.jsonl")

    lock = threading.Lock()
    extract_only, maintained, logs = [], [], {}
    with ThreadPoolExecutor(max_workers=len(runs)) as pool:
        jobs = {}
        for run in runs:
            candidates = [c for u in run["units"] for c in units[(run["run"], u["task"])]["candidates"]]
            extract_only += [bank_skill(c, run, i + 1, []) for i, c in enumerate(candidates)]
            jobs[run["run"]] = pool.submit(maintain, run, candidates, args.out, profile, contract, tpl, run["terms"], encoder, lock)
        for name, job in jobs.items():
            bank, logs[name] = job.result()
            maintained += bank
    for run in runs:
        write_json(args.out / run["run"] / "skills-extract-only.json", [s for s in extract_only if s["origin_run"] == run["run"]])
        write_json(args.out / run["run"] / "skills-maintained.json", [s for s in maintained if s["origin_run"] == run["run"]])
    manifest["finished_at_utc"] = utc_now()
    manifest["counts"] = {
        "extract_only": len(extract_only),
        "maintained_active": sum(1 for s in maintained if s["status"] == "active"),
        "maintenance_actions": {name: [e["action"] for e in log] for name, log in logs.items()},
    }
    write_json(manifest_path, manifest)
    print(json.dumps(manifest["counts"], indent=1))


if __name__ == "__main__":
    main()
