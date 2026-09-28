"""Offline replay of task/event skill retrieval on recorded trajectories.

No solver and no model service is used: only the pinned local MiniLM.  Each
tool batch that the live sidecar would have seen is rebuilt from the recorded
upstream requests (or, for historical traces without a sidecar, from the
imported steps), and the current query construction is compared with the
fixes proposed in docs/DECISIONS.md P2.

Relevance labels are hand-written trigger predicates per skill (LABELS below):
a skill is relevant at a tool batch when its predicate matches that batch's
observation or action.  Task labels list the instances a task skill applies to.

Two eligibility settings are reported:
  production  skills from the same instance are excluded (live D08 rule), so
              only genuinely cross-task reuse can count as relevant;
  crossrun    only skills extracted from this exact run are excluded, so the
              same task's other runs supply positives (diagnostic only; this
              would be leakage in a real evaluation).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeskill_rebuild.openclaw_overlay import _complete_batches  # noqa: E402
from codeskill_rebuild.openclaw_sidecar_retrieval import _first_user_text, _message_text  # noqa: E402
from codeskill_rebuild.retrieval import MiniLMEncoder, query_record, skill_index_record, token_budgeted_text  # noqa: E402
from codeskill_rebuild.retrieval_query import (  # noqa: E402
    ERROR_RE,
    event_query_fields,
    plain_action,
    task_query_fields,
)
from codeskill_rebuild.types import canonical_json  # noqa: E402

HOME = Path.home() / "ray"
SWE = HOME / "tmp/codeskill-swe-pilot-20260928"
TB2 = HOME / "tmp/r015-c-only-formal-20260914-01"
HIST = HOME / "codeskill-rebuild-20260905/runs/m2-r014-source-import-20260911-01/trajectories/normalized"
MINILM_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
TASK_THRESHOLD, TASK_LIMIT = 0.45, 2
EVENT_THRESHOLD, EVENT_LIMIT = 0.50, 1
NO_SKILL_RUNS = ("swe-baseline", "tb2-hist")  # trajectories that never saw an injected skill
BEHAVIOR_WINDOW = 3  # agent actions after a trigger in which the skill's action counts as already done


# Trigger predicates: obs/act/any are regexes over the batch observation, the
# plain-text action, or both.  "scope" limits a content skill to the instances
# where its situation can occur; error-condition skills have no scope and may
# match any task.  "tasks" lists the instances a task skill applies to.
_TUPLE = ["sphinx-doc__sphinx-8265", "sphinx-doc__sphinx-9367"]
_GIT = ["fix-git", "git-leak-recovery"]
LABELS: dict[str, dict[str, Any]] = {
    # SWE: every skill is a per-issue fix; only the tuple pair spans two issues.
    "skill-48ef05941baaf77a": {"scope": _TUPLE, "any": r"visit_Tuple|_UnparseVisitor|pycode/ast\.py"},
    "skill-f991ec900dac4757": {"scope": _TUPLE, "any": r"visit_Tuple|_UnparseVisitor|pycode/ast\.py"},
    "skill-bb5fc1c6c2340641": {"scope": ["sphinx-doc__sphinx-7748"], "any": r"_find_signature|DocstringSignatureMixin"},
    "skill-ef5d7509044ecbbc": {"scope": ["sphinx-doc__sphinx-7757"], "any": r"posonlyargs|signature_from_str"},
    "skill-3e762bc448151a8c": {"scope": ["sphinx-doc__sphinx-9281"], "any": r"object_description|enum\.Enum"},
    "skill-5dd95aeceb90cbe3": {"scope": ["django__django-11790"], "any": r"maxlength|widget_attrs"},
    "skill-96b1690bb61bd610": {"scope": ["django__django-12193"], "any": r"CheckboxInput|SplitArrayWidget"},
    "skill-734321510c06d90e": {"scope": ["django__django-12276"], "any": r"use_required_attribute"},
    "skill-f80955c10587ff9d": {"scope": ["django__django-12406"], "any": r"empty_label|RadioSelect"},
    "skill-ace498085ea205e9": {"scope": ["django__django-12713"], "any": r"formfield_for_manytomany"},
    # TB2 error-condition skills (any task).
    "skill-c9ab3067228f3be8": {"obs": r"\bxz\b[^\n]{0,80}(not found|Cannot exec|No such file)|(not found|Cannot exec)[^\n]{0,40}\bxz\b"},
    "skill-be8ef29bb538590f": {"obs": r"xxd: (command )?not found"},
    "skill-0ac88c0097a1706d": {"obs": r"(?i)committer identity unknown|please tell me who you are"},
    "skill-78dc12c0a9800777": {"obs": r"(?i)committer identity unknown|please tell me who you are"},
    "skill-09619bae1d1d9a0f": {"obs": r"reset: moving to"},
    "skill-df3cc6946dff09cc": {"obs": r"expected ';' before"},
    "skill-0732d06952a4c12d": {"obs": r"auth-ops-list|password-file"},
    "skill-188568bc963e9e65": {"obs": r"BackendUnavailable|setuptools\.backends"},
    "skill-1bf9b92c21eefced": {"obs": r"BackendUnavailable|setuptools\.backends"},
    "skill-3a89a280a78b8c02": {"obs": r"\b(gcc|cc|make|g\+\+): (command )?not found"},
    "skill-209e23ab421280b4": {"obs": r"\bss: (command )?not found"},
    "skill-32ab337b39b59ee1": {"obs": r"\b(python3?|node|ruby|perl): (command )?not found"},
    # TB2 content skills (scoped).
    "skill-6df4629461469186": {"scope": _GIT, "act": r"git (reflog|stash list|log --all|branch -a)"},
    "skill-56d82b00de1af58a": {"scope": _GIT, "act": r"git (reflog|stash list|log --all|branch -a)"},
    "skill-4f60a65b78bd7bef": {"scope": ["cancel-async-tasks"], "any": r"Semaphore"},
    "skill-3f53174a02661ec4": {"scope": ["cobol-modernization"], "act": r"\b(od|xxd|hexdump|cat|head)\b[^\n]*\.DAT"},
    "skill-e0c1a422651f6b6a": {"scope": ["cobol-modernization"], "any": r"PIC 9"},
    "skill-e9f314c40d180d00": {"scope": ["cobol-modernization"], "any": r"PIC 9"},
    "skill-b8b5afd03a3d1d82": {"scope": ["headless-terminal"], "any": r"pexpect|PS1|--rcfile"},
    "skill-4c575438f012d506": {"scope": ["kv-store-grpc"], "any": r"grpc_tools\.protoc|_pb2_grpc"},
    "skill-abbf7e53ee3d44af": {"scope": ["schemelike-metacircular-eval"], "any": r"cadddr|proc-env"},
    "skill-2140617874793f9a": {"scope": ["fix-ocaml-gc"], "any": r"pool_sweep|Whsize_hd|Wosize_hd"},
    # Task skills.
    "skill-8f78802d25e06c07": {"tasks": _TUPLE},
    "skill-4a995b54c50f29b9": {"tasks": _GIT},
    "skill-bf9d7a43678bf335": {"tasks": ["build-pmars", "polyglot-c-py"]},
}

# Response predicates: the action each skill recommends, matched against the agent's next
# actions (the whole trajectory for task skills).  Skills whose trigger already is the
# action (od on .DAT files) or whose advice has no command signature have none.
ACTIONS = {
    "skill-5dd95aeceb90cbe3": r"attrs\[\\?['\"]maxlength",
    "skill-96b1690bb61bd610": r"\{\*\*\(?attrs|attrs\.copy\(\)|dict\(attrs",
    "skill-734321510c06d90e": r"edit \{.*use_required_attribute",
    "skill-f80955c10587ff9d": r"empty_label = None",
    "skill-ace498085ea205e9": r"widget.{0,4} not in kwargs",
    "skill-bb5fc1c6c2340641": r"_additional_signatures|sigs\.append",
    "skill-ef5d7509044ecbbc": r"edit \{.*posonlyargs.*default",
    "skill-48ef05941baaf77a": r"edit \{.*visit_Tuple",
    "skill-f991ec900dac4757": r"edit \{.*visit_Tuple",
    "skill-3e762bc448151a8c": r"isinstance\(\w+, enum\.Enum\)",
    "skill-8f78802d25e06c07": r"edit \{.*visit_Tuple",
    "skill-c9ab3067228f3be8": r"lzma|tarfile.*r:xz",
    "skill-be8ef29bb538590f": r"\bod\b|hexdump|python3? -c",
    "skill-0ac88c0097a1706d": r"git config (--global |--local )?user\.(email|name)|GIT_(AUTHOR|COMMITTER)_",
    "skill-78dc12c0a9800777": r"git config (--global |--local )?user\.(email|name)|GIT_(AUTHOR|COMMITTER)_",
    "skill-09619bae1d1d9a0f": r"git reflog|git (show|cherry-pick) [0-9a-f]{6,}",
    "skill-4a995b54c50f29b9": r"git reflog",
    "skill-b8b5afd03a3d1d82": r"--rcfile",
    "skill-4c575438f012d506": r"(cat|read|head|sed -n)[^\n]*_pb2(_grpc)?\.py",
    "skill-abbf7e53ee3d44af": r"list4|\(cons [^\n]*\(cons ",
    "skill-2140617874793f9a": r"p \+= wh",
    "skill-0732d06952a4c12d": r"-a \. -P \.|-P \. -a \.",
    "skill-188568bc963e9e65": r"setuptools\.build_meta",
    "skill-1bf9b92c21eefced": r"setuptools\.build_meta",
    "skill-3a89a280a78b8c02": r"apt-get download|dpkg(-deb)? -x",
    "skill-e0c1a422651f6b6a": r"ljust\(|replace\(\s*b?['\"] ['\"]",
    "skill-e9f314c40d180d00": r"ljust\(|replace\(\s*b?['\"] ['\"]",
    "skill-6df4629461469186": r"git cherry-pick|git show [0-9a-f]{6,}",
    "skill-56d82b00de1af58a": r"git cherry-pick|git show [0-9a-f]{6,}",
    "skill-209e23ab421280b4": r"/proc/net/tcp|netstat|lsof|nc -z",
    "skill-32ab337b39b59ee1": r"apt-get download|dpkg(-deb)? -x|which python|ls /usr/bin/python",
    "skill-bf9d7a43678bf335": r"apt-get download|dpkg(-deb)? -x|deb\.debian\.org/debian/pool",
}
for _skill_id, _action in ACTIONS.items():
    LABELS[_skill_id]["action"] = _action


# ---------------------------------------------------------------- loading


def _run(kind: str, benchmark: str, instance: str, run: str, path: Path) -> dict[str, Any]:
    return {"kind": kind, "benchmark": benchmark, "instance": instance, "run": run, "path": str(path)}


def discover_runs() -> list[dict[str, Any]]:
    runs = []
    for arm in ("baseline", "codeskill"):
        for trial in sorted((SWE / "trials" / arm).iterdir()):
            runs.append(_run("sidecar", "swe", trial.name.split("-", 1)[1], "swe-" + arm, trial / "sidecar"))
    for trial in sorted(glob.glob(str(TB2 / "attempts/attempt-003/round-*/*/official-harbor"))):
        parts = Path(trial).parts
        runs.append(_run("sidecar", "tb2", parts[-2], "tb2-" + parts[-3].replace("round-", "r"), Path(trial) / "sidecar"))
    for path in sorted(HIST.glob("*.json")):
        runs.append(_run("steps", "tb2", path.stem, "tb2-hist", path))
    return runs


def _steps_to_native(trace: dict[str, Any], system: str) -> list[dict[str, Any]]:
    """Rebuild OpenAI-shaped native messages from an imported historical trace."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for step in trace["steps"]:
        parts = [item for item in step["content"] if isinstance(item, dict)]
        text = "".join(item.get("text", "") for item in parts if item.get("type") == "text")
        if step["role"] == "user":
            messages.append({"role": "user", "content": text})
        elif step["role"] == "assistant":
            calls = [
                {
                    "id": item["tool_call_id"].replace("_", ""),
                    "type": "function",
                    "function": {"name": item["tool_name"], "arguments": json.dumps(item["arguments"], separators=(",", ":"))},
                }
                for item in parts
                if item.get("type") == "tool_call"
            ]
            thinking = "\n".join(item.get("text", "") for item in parts if item.get("type") == "thinking")
            messages.append({"role": "assistant", "content": text.strip() or None, "reasoning_content": thinking, "tool_calls": calls or None})
        elif step["role"] == "toolResult":
            messages.append({"role": "tool", "content": text, "tool_call_id": step["tool_result"]["tool_call_id"].replace("_", "")})
    return messages


def load_batches(run: dict[str, Any], tb2_system: str) -> dict[str, Any]:
    """Return the first request's task inputs and every tool batch, deduplicated by call IDs."""
    if run["kind"] == "sidecar":
        requests = [json.loads(Path(p).read_text())["native_request"]["messages"] for p in sorted(glob.glob(run["path"] + "/upstream_requests/*.json"))]
    else:
        requests = [_steps_to_native(json.loads(Path(run["path"]).read_text()), tb2_system)]
    first = requests[0]
    batches, seen = [], set()
    for messages in requests:
        for anchor in _complete_batches(messages):
            key = tuple(anchor["tool_call_ids"])
            if key in seen:
                continue
            seen.add(key)
            assistant = messages[anchor["assistant_index"]]
            results = [messages[i] for i in anchor["tool_result_indices"]]
            batches.append({
                "tool_call_ids": anchor["tool_call_ids"],
                "assistant": assistant,
                "results": results,
                "first_user": _first_user_text(messages),
                "ordinal": len(batches),
            })
    original = next(m for m in first if m.get("role") == "user")
    system = "\n".join(_message_text(m) for m in first if m.get("role") == "system")
    return {**run, "original_user": _message_text(original), "system": system, "batches": batches}


def load_skills(skills_dir: Path | None = None, variant: str = "maintained") -> list[dict[str, Any]]:
    """The live banks by default; with skills_dir, a P1 bank (run_p1_extraction.py output)."""
    if skills_dir is not None:
        return [s for run in ("swe-codeskill", "tb2-r1", "tb2-r2") for s in json.loads((skills_dir / run / f"skills-{variant}.json").read_text())]
    skills = []
    swe = json.loads((SWE / "pilot-state.json").read_text())
    for skill in swe["rounds"]["1"]["bank"]["skills"]:
        skills.append({**skill, "origin_run": "swe-codeskill"})
    tb2 = json.loads((TB2 / "state.json").read_text())
    for round_id in ("1", "2"):
        for skill in tb2["rounds"][round_id]["bank"]["skills"]:
            skills.append({**skill, "origin_run": "tb2-r" + round_id})
    return skills


# ---------------------------------------------------------------- queries


def observation(batch: dict[str, Any]) -> str:
    return "\n".join(_message_text(m) for m in batch["results"])


def event_fields(variant: str, traj: dict[str, Any], batch: dict[str, Any]) -> dict[str, str]:
    assistant = batch["assistant"]
    if variant == "current":
        # Byte-for-byte the construction in FrozenBankSelectors.select_event.
        recent_action = _message_text(assistant)
        if assistant.get("tool_calls") is not None:
            recent_action += "\n" + canonical_json(assistant["tool_calls"])
        return {
            "observation_errors_tests": observation(batch),
            "recent_action": recent_action,
            "public_reasoning": _message_text(assistant),
            "task_context": batch["first_user"],
        }
    # The fixed variants are the deployed P2 construction (retrieval_query).
    fields = event_query_fields(batch["first_user"], assistant, batch["results"])
    if variant == "fixed_no_task":
        fields["task_context"] = ""
    return fields


def task_fields(variant: str, traj: dict[str, Any]) -> dict[str, str]:
    if variant == "current":
        return {"goal_problem": traj["original_user"], "repo_context": traj["system"]}
    return task_query_fields(traj["original_user"], traj["instance"])


def skill_text(tokenizer: Any, skill: dict[str, Any], variant: str, max_len: int) -> str:
    if variant == "full":
        return skill_index_record(tokenizer, skill, max_len)["text"]
    fields = [("Title:", skill["title"], 0.3), ("When to apply:", skill["when_to_apply"], 0.7)]
    return token_budgeted_text(tokenizer, fields, max_len)["text"]


# ---------------------------------------------------------------- labels and eligibility


def relevant_event(skill_id: str, instance: str, obs: str, act: str) -> bool:
    label = LABELS.get(skill_id, {})
    if "scope" in label and instance not in label["scope"]:
        return False
    if "obs" in label and re.search(label["obs"], obs):
        return True
    if "act" in label and re.search(label["act"], act):
        return True
    return "any" in label and bool(re.search(label["any"], obs + "\n" + act))


def eligible(skills: list[dict[str, Any]], traj: dict[str, Any], setting: str, granularity: str) -> list[dict[str, Any]]:
    def excluded(skill: dict[str, Any]) -> bool:
        sources = skill["provenance"]["source_instance_ids"]
        if setting == "production":
            return traj["instance"] in sources
        return traj["instance"] in sources and skill["origin_run"] == traj["run"]

    pool = [s for s in skills if s["origin_run"].split("-")[0] == traj["benchmark"] and s["granularity"] == granularity]
    keep = [s for s in pool if not excluded(s)]
    active = [s for s in keep if s["status"] == "active"]
    # A superseded version stays retrievable only when every successor is excluded,
    # mirroring how the live frozen bank still held it before the merge.
    for skill in keep:
        if skill["status"] == "superseded":
            successors = [s for s in pool if skill["skill_id"] in (s["provenance"].get("parent_skill_ids") or [])]
            if successors and all(excluded(s) for s in successors):
                active.append(skill)
    return active


# ---------------------------------------------------------------- scoring


def auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skills", type=Path, help="P1 output directory; default is the live banks")
    parser.add_argument("--variant", choices=("extract-only", "maintained"), default="maintained")
    parser.add_argument("--labels", type=Path, help="JSON labels for new skills, merged into LABELS (write before scoring)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.labels:
        LABELS.update(json.loads(args.labels.read_text()))

    encoder = MiniLMEncoder(revision=MINILM_REVISION)
    encoder_meta = encoder.load()
    tokenizer, max_len = encoder._model.tokenizer, encoder._model.max_seq_length

    tb2_system = ""
    runs = discover_runs()
    trajs = []
    for run in runs:
        if run["kind"] == "sidecar" and not glob.glob(run["path"] + "/upstream_requests/*.json"):
            continue
        traj = load_batches(run, tb2_system)
        if run["benchmark"] == "tb2" and not tb2_system:
            tb2_system = traj["system"]
        trajs.append(traj)
    skills = load_skills(args.skills, args.variant)
    if args.skills and (unlabelled := [s["skill_id"] for s in skills if s["skill_id"] not in LABELS]):
        sys.exit(f"skills without labels: {unlabelled}")

    def encode(texts: list[str]) -> list[list[float]]:
        return encoder.encode(texts)

    skill_vecs = {}
    for variant in ("full", "when"):
        vecs = encode([skill_text(tokenizer, s, variant, max_len) for s in skills])
        for skill, vec in zip(skills, vecs):
            skill_vecs[(variant, skill["skill_id"])] = vec

    def dot(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    # ---- event replay
    event_variants = ("current", "fixed", "fixed_no_task")
    query_texts, keys = [], []
    for ti, traj in enumerate(trajs):
        for batch in traj["batches"]:
            for variant in event_variants:
                query_texts.append(query_record(tokenizer, "event", event_fields(variant, traj, batch), max_len)["text"])
                keys.append((ti, batch["ordinal"], variant))
    query_vecs = dict(zip(keys, encode(query_texts)))
    query_text_by_key = dict(zip(keys, query_texts))

    rows = []  # one row per (setting, traj, batch, query variant, skill variant)
    for setting in ("production", "crossrun"):
        for ti, traj in enumerate(trajs):
            pool = eligible(skills, traj, setting, "event")
            for batch in traj["batches"]:
                obs, act = observation(batch), plain_action(batch["assistant"])
                is_error = bool(ERROR_RE.search(obs))
                rel = {s["skill_id"] for s in pool if relevant_event(s["skill_id"], traj["instance"], obs, act)}
                for qv in event_variants:
                    qvec = query_vecs[(ti, batch["ordinal"], qv)]
                    for sv in ("full", "when"):
                        scored = sorted(((dot(qvec, skill_vecs[(sv, s["skill_id"])]), s["skill_id"]) for s in pool), reverse=True)
                        rows.append({
                            "setting": setting, "traj": ti, "run": traj["run"], "instance": traj["instance"],
                            "benchmark": traj["benchmark"], "batch": batch["ordinal"], "query": qv, "skill_side": sv,
                            "is_error": is_error, "relevant": sorted(rel), "scored": scored,
                        })
    event_rows_path = args.out / "event-rows.jsonl"
    with event_rows_path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

    def summarize(sel: list[dict[str, Any]], gate: str, threshold: float) -> dict[str, Any]:
        trajs_in = {r["traj"] for r in sel}
        triggered = [r for r in sel if gate == "all" or r["is_error"]]
        injections = [(r, r["scored"][0]) for r in triggered if r["scored"] and r["scored"][0][0] >= threshold]
        correct = [1 for r, top in injections if top[1] in r["relevant"]]
        relevant_rows = [r for r in sel if r["relevant"]]
        recalled = [1 for r in relevant_rows if (gate == "all" or r["is_error"]) and r["scored"][0][0] >= threshold and r["scored"][0][1] in r["relevant"]]
        hit1 = [1 for r in relevant_rows if r["scored"][0][1] in r["relevant"]]
        unique = {(r["traj"], top[1]) for r, top in injections}
        pos = [s for r in sel for s, sid in r["scored"] if sid in r["relevant"]]
        neg = [s for r in sel for s, sid in r["scored"] if sid not in r["relevant"]]
        return {
            "trajectories": len(trajs_in),
            "batches": len(sel),
            "triggers_per_traj": len(triggered) / max(1, len(trajs_in)),
            "injections_per_traj": len(injections) / max(1, len(trajs_in)),
            "unique_injections": len(unique),
            "precision": (len(correct) / len(injections)) if injections else None,
            "relevant_batches": len(relevant_rows),
            "relevant_batches_passing_gate": sum(1 for r in relevant_rows if gate == "all" or r["is_error"]),
            "recall": (len(recalled) / len(relevant_rows)) if relevant_rows else None,
            "hit_at_1_relevant": (len(hit1) / len(relevant_rows)) if relevant_rows else None,
            "auc": auc(pos, neg),
            "mean_top1_score": sum(r["scored"][0][0] for r in sel if r["scored"]) / max(1, sum(1 for r in sel if r["scored"])),
        }

    event_summary = []
    for setting in ("production", "crossrun"):
        for bench in ("swe", "tb2"):
            for qv in event_variants:
                for sv in ("full", "when"):
                    sel = [r for r in rows if r["setting"] == setting and r["benchmark"] == bench and r["query"] == qv and r["skill_side"] == sv]
                    for gate in ("all", "error"):
                        for threshold in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
                            event_summary.append({"setting": setting, "benchmark": bench, "query": qv, "skill_side": sv, "gate": gate, "threshold": threshold, **summarize(sel, gate, threshold)})

    # ---- task replay
    task_rows = []
    task_texts, task_keys = [], []
    for ti, traj in enumerate(trajs):
        for variant in ("current", "fixed"):
            task_texts.append(query_record(tokenizer, "task", task_fields(variant, traj), max_len)["text"])
            task_keys.append((ti, variant))
    task_vecs = dict(zip(task_keys, encode(task_texts)))
    task_text_by_key = dict(zip(task_keys, task_texts))
    for setting in ("production", "crossrun"):
        for ti, traj in enumerate(trajs):
            pool = eligible(skills, traj, setting, "task")
            for qv in ("current", "fixed"):
                for sv in ("full", "when"):
                    scored = sorted(((dot(task_vecs[(ti, qv)], skill_vecs[(sv, s["skill_id"])]), s["skill_id"]) for s in pool), reverse=True)
                    rel = [sid for _, sid in scored if traj["instance"] in LABELS.get(sid, {}).get("tasks", [])]
                    injected = [sid for score, sid in scored[:TASK_LIMIT] if score >= TASK_THRESHOLD]
                    task_rows.append({"setting": setting, "run": traj["run"], "instance": traj["instance"], "query": qv, "skill_side": sv, "scored": scored, "relevant": rel, "injected": injected})

    # ---- behavior: does an agent without skills already take the skill's action when its
    # trigger occurs?  A high rate marks a skill as routine (docs/DECISIONS.md P1).
    behavior = []
    for skill in skills:
        label = LABELS.get(skill["skill_id"], {})
        if "action" not in label:
            continue
        pool = [(ti, t) for ti, t in enumerate(trajs)
                if t["run"] in NO_SKILL_RUNS and t["benchmark"] == skill["origin_run"].split("-")[0]]
        triggers = done = 0
        seen: set[int] = set()
        for ti, traj in pool:
            actions = [plain_action(b["assistant"]) for b in traj["batches"]]
            if skill["granularity"] == "task":
                if traj["instance"] in label.get("tasks", []):
                    triggers += 1
                    seen.add(ti)
                    done += bool(re.search(label["action"], "\n".join(actions)))
                continue
            for i, batch in enumerate(traj["batches"]):
                if relevant_event(skill["skill_id"], traj["instance"], observation(batch), actions[i]):
                    triggers += 1
                    seen.add(ti)
                    done += bool(re.search(label["action"], "\n".join(actions[i + 1:i + 1 + BEHAVIOR_WINDOW])))
        behavior.append({"skill_id": skill["skill_id"], "title": skill["title"], "granularity": skill["granularity"],
                         "status": skill["status"], "triggers": triggers, "trajectories": len(seen), "already_done": done})

    # ---- live cross-check: replayed "current" query text must equal what the sidecar logged
    live_check = {"event_checked": 0, "event_equal": 0, "task_checked": 0, "task_equal": 0}
    live_injections: list[dict[str, Any]] = []
    for ti, traj in enumerate(trajs):
        if traj["kind"] != "sidecar":
            continue
        state_path = Path(traj["path"]) / "overlay-state.json"
        if not state_path.is_file():
            continue
        state = json.loads(state_path.read_text())
        by_ids = {tuple(b["tool_call_ids"]): b["ordinal"] for b in traj["batches"]}
        for processed in state.get("processed_batches") or []:
            query = ((processed.get("selection_metadata") or {}).get("query") or {}).get("query") or {}
            ordinal = by_ids.get(tuple(processed["anchor"]["tool_call_ids"]))
            if ordinal is None or "text" not in query:
                continue
            live_check["event_checked"] += 1
            live_check["event_equal"] += query["text"] == query_text_by_key[(ti, ordinal, "current")]
        # Label every live injection too; the SWE ones can be compared with the
        # Fig.8 evolve verdicts, which called all 14 unrelated.
        for event in state.get("events") or []:
            ordinal = by_ids.get(tuple(event["anchor"]["tool_call_ids"]))
            batch = traj["batches"][ordinal]
            live_injections.append({
                "run": traj["run"], "instance": traj["instance"], "batch": ordinal, "skill_id": event["skill"].get("skill_id"),
                "title": event["skill"]["title"],
                "relevant": relevant_event(event["skill"].get("skill_id", ""), traj["instance"], observation(batch), plain_action(batch["assistant"])),
            })
        task_query = ((state.get("task") or {}).get("query") or {}).get("query") or {}
        if "text" in task_query:
            live_check["task_checked"] += 1
            live_check["task_equal"] += task_query["text"] == task_text_by_key[(ti, "current")]

    # ---- sample query texts for the report
    samples = {}
    for ti, traj in enumerate(trajs):
        if traj["run"] in ("swe-codeskill", "tb2-r2") and traj["instance"] in ("sphinx-doc__sphinx-9367", "git-leak-recovery"):
            samples[traj["run"] + ":" + traj["instance"]] = {
                "task": {v: task_text_by_key[(ti, v)] for v in ("current", "fixed")},
                "event_batch_3": {v: query_text_by_key[(ti, 3, v)] for v in event_variants} if len(traj["batches"]) > 3 else None,
            }

    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True).stdout.strip())
    result = {
        "kind": "offline_retrieval_eval",
        "git_commit": commit,
        "git_dirty": dirty,
        "skills_source": {"dir": str(args.skills), "variant": args.variant} if args.skills else "live banks",
        "encoder": encoder_meta,
        "trajectories": [{"run": t["run"], "instance": t["instance"], "batches": len(t["batches"])} for t in trajs],
        "skills": [{"skill_id": s["skill_id"], "version": s["version"], "granularity": s["granularity"], "status": s["status"], "origin_run": s["origin_run"], "sources": s["provenance"]["source_instance_ids"], "title": s["title"]} for s in skills],
        "labels": LABELS,
        "live_check": live_check,
        "live_injections": live_injections,
        "event_summary": event_summary,
        "task_rows": task_rows,
        "behavior": behavior,
        "samples": samples,
    }
    (args.out / "result.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(json.dumps({"live_check": live_check, "live_injections": len(live_injections), "live_injections_relevant": sum(i["relevant"] for i in live_injections), "trajectories": len(trajs), "batches": sum(len(t["batches"]) for t in trajs)}, indent=1))


if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    main()
