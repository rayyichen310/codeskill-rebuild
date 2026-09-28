"""Offline evaluation of a Jev relevance stage after MiniLM event retrieval.

Reads the replay rows written by eval_retrieval_offline.py (fixed query, full
skill text), keeps tool batches whose MiniLM top-1 clears a prefilter, and asks
Jev (TypeSafe System One API) which shortlisted skill, if any, applies to the
current situation.  Results are compared with MiniLM-only injection against the
same trigger-predicate labels.  No solver runs; the only model is Jev.

  --dry-run   build requests, print counts and one sample; no network.
  (default)   call the API (TYPESAFE_API_KEY), caching every response so a
              rerun or an interrupted run never repeats a paid call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_retrieval_offline as base  # noqa: E402
from codeskill_rebuild.relevance_judge import JUDGE_PROMPT, judge_request  # noqa: E402
from codeskill_rebuild.retrieval_query import judge_state  # noqa: E402

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"  # pinned: thresholds tuned on one version must not drift with an alias
SHORTLIST = 3
PREFILTERS = (0.35, 0.40, 0.45)
TASK_PREFILTER = 0.20
FIT_THRESHOLDS = (0.3, 0.5, 0.7)

def body_key(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def build(out: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One item per (setting, trajectory, batch) whose top-1 clears the lowest prefilter."""
    rows = [json.loads(line) for line in (out / "event-rows.jsonl").open()]
    rows = [r for r in rows if r["query"] == "fixed" and r["skill_side"] == "full"]
    result = json.loads((out / "result.json").read_text())
    # Judge the same bank the retrieval eval scored (older results predate skills_source).
    source = result.get("skills_source", "live banks")
    loaded = base.load_skills() if source == "live banks" else base.load_skills(Path(source["dir"]), source["variant"])
    skills = {s["skill_id"]: s for s in loaded}
    trajs, tb2_system = [], ""
    for run in base.discover_runs():
        if run["kind"] == "sidecar" and not list(Path(run["path"]).glob("upstream_requests/*.json")):
            continue
        traj = base.load_batches(run, tb2_system)
        if run["benchmark"] == "tb2" and not tb2_system:
            tb2_system = traj["system"]
        trajs.append(traj)
    items = []
    for r in rows:
        if not r["scored"] or r["scored"][0][0] < min(PREFILTERS):
            continue
        traj = trajs[r["traj"]]
        batch = traj["batches"][r["batch"]]
        shortlist = [skills[sid] for _, sid in r["scored"][:SHORTLIST]]
        # Same construction as the deployed sidecar (retrieval_query + relevance_judge).
        body = judge_request("event", judge_state(batch["first_user"], batch["assistant"], batch["results"]), shortlist, MODEL)
        items.append({
            "setting": r["setting"], "benchmark": r["benchmark"], "traj": r["traj"], "batch": r["batch"],
            "top1": r["scored"][0][0], "shortlist": [sid for _, sid in r["scored"][:SHORTLIST]],
            "relevant": r["relevant"], "key": body_key(body), "body": body,
        })
    task_items = []
    for r in result["task_rows"]:
        if r["query"] != "fixed" or r["skill_side"] != "full":
            continue
        ranked = [(score, sid) for score, sid in r["scored"] if score >= TASK_PREFILTER][:SHORTLIST]
        if not ranked:
            continue
        traj = next(t for t in trajs if t["run"] == r["run"] and t["instance"] == r["instance"])
        body = judge_request("task", judge_state(traj["original_user"], None, []), [skills[sid] for _, sid in ranked], MODEL)
        task_items.append({
            "setting": r["setting"], "run": r["run"], "instance": r["instance"], "shortlist": [sid for _, sid in ranked],
            "scores": [score for score, _ in ranked], "relevant": r["relevant"], "key": body_key(body), "body": body,
        })
    totals = {}
    for setting in ("production", "crossrun"):
        for bench in ("swe", "tb2"):
            sel = [r for r in rows if r["setting"] == setting and r["benchmark"] == bench]
            totals[(setting, bench)] = {
                "trajectories": len({r["traj"] for r in sel}),
                "relevant_batches": sum(1 for r in sel if r["relevant"]),
            }
    return items, task_items, totals


def call(body: dict[str, Any], api_key: str) -> dict[str, Any]:
    import httpx

    for attempt in range(6):
        started = time.monotonic()
        response = httpx.post(ENDPOINT, json=body, headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
        latency = time.monotonic() - started
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(float(response.headers.get("retry-after", 2 ** attempt)))
            continue
        response.raise_for_status()
        return {"response": response.json(), "latency_s": latency}
    raise RuntimeError(f"Jev request failed after retries: {response.status_code} {response.text[:300]}")


def summarize(items: list[dict[str, Any]], answers: dict[str, dict[str, Any]], totals: dict[Any, Any]) -> list[dict[str, Any]]:
    out = []
    for (setting, bench), total in totals.items():
        for prefilter in PREFILTERS:
            sel = [i for i in items if i["setting"] == setting and i["benchmark"] == bench and i["top1"] >= prefilter]
            variants: dict[str, list[tuple[dict[str, Any], str]]] = {"minilm_top1": [(i, i["shortlist"][0]) for i in sel]}
            for tau in (None, *FIT_THRESHOLDS):
                picks = []
                for i in sel:
                    answer = answers[i["key"]]["response"]["answers"]
                    choice = answer["pick"]["choice"]
                    if choice == "none":
                        continue
                    index = int(choice.split("_")[1])
                    if tau is not None and answer[f"fits_{index}"]["noul"] < tau:
                        continue
                    picks.append((i, i["shortlist"][index]))
                variants["jev_choice" if tau is None else f"jev_choice_fits>={tau}"] = picks
            for name, picks in variants.items():
                correct = sum(1 for i, sid in picks if sid in i["relevant"])
                out.append({
                    "setting": setting, "benchmark": bench, "prefilter": prefilter, "variant": name,
                    "candidates": len(sel), "injections_per_traj": len(picks) / max(1, total["trajectories"]),
                    "precision": correct / len(picks) if picks else None,
                    "recall": correct / total["relevant_batches"] if total["relevant_batches"] else None,
                })
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True, help="directory holding event-rows.jsonl from eval_retrieval_offline.py")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    items, task_items, totals = build(args.out)
    unique = {i["key"]: i["body"] for i in items + task_items}
    counts = {f"{s}/{b}@{p}": sum(1 for i in items if i["setting"] == s and i["benchmark"] == b and i["top1"] >= p) for (s, b) in totals for p in PREFILTERS}
    print(json.dumps({"requests": len(unique), "state_chars_mean": sum(len(json.dumps(b)) for b in unique.values()) // max(1, len(unique)), "by_prefilter": counts}, indent=1))
    if args.dry_run:
        print(json.dumps(next(iter(unique.values())), indent=1, ensure_ascii=False)[:4000])
        return

    api_key = os.environ["TYPESAFE_API_KEY"]
    cache_path = args.out / "jev-responses.jsonl"
    answers = {}
    if cache_path.exists():
        for line in cache_path.open():
            record = json.loads(line)
            answers[record["key"]] = record
    todo = [(k, b) for k, b in unique.items() if k not in answers]
    print(f"cached {len(answers)}, calling {len(todo)}")
    with cache_path.open("a") as handle, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for key, result in zip((k for k, _ in todo), pool.map(lambda kb: call(kb[1], api_key), todo)):
            record = {"key": key, **result}
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            answers[key] = record
    summary = summarize(items, answers, totals)
    task_summary = []
    for i in task_items:
        choice = answers[i["key"]]["response"]["answers"]["pick"]["choice"]
        picked = None if choice == "none" else i["shortlist"][int(choice.split("_")[1])]
        task_summary.append({k: i[k] for k in ("setting", "run", "instance", "shortlist", "scores", "relevant")} | {"picked": picked})
    latencies = sorted(answers[k]["latency_s"] for k in unique)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True).stdout.strip())
    models = sorted({answers[k]["response"]["model"] for k in unique})
    usage = sum(answers[k]["response"]["usage"]["input_tokens"] for k in unique)
    result = {
        "kind": "offline_relevance_judge_eval", "git_commit": commit, "git_dirty": dirty, "models": models,
        "requests": len(unique), "input_tokens": usage,
        "latency_s": {"p50": latencies[len(latencies) // 2], "p90": latencies[int(len(latencies) * 0.9)], "max": latencies[-1]},
        "judge_prompt": JUDGE_PROMPT,
        "summary": summary,
        "task_summary": task_summary,
        "items": [{k: v for k, v in i.items() if k != "body"} for i in items],
    }
    (args.out / "judge-result.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({k: result[k] for k in ("models", "requests", "input_tokens", "latency_s")}, indent=1))


if __name__ == "__main__":
    main()
