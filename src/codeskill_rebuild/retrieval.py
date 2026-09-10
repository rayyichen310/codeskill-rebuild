"""True MiniLM retrieval with source-aware filtering performed by SkillBank."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class Encoder(Protocol):
    def encode(self, values: list[str]) -> list[list[float]]: ...


def cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def skill_index_text(skill: dict[str, Any]) -> str:
    """Human-readable complete representation; not the encoded retrieval text."""
    rules = " ".join(skill.get("rules", []))
    return "\n".join(
        [
            "Title: " + str(skill.get("title", "")),
            "When to apply: " + str(skill.get("when_to_apply", "")),
            "Rules: " + rules,
        ]
    )


def token_budgeted_text(tokenizer: Any, fields: list[tuple[str, str, float]], max_seq_length: int) -> dict[str, Any]:
    """Allocate actual tokenizer pieces across fields and prove their presence.

    Each non-empty field receives at least one content piece.  If labels,
    separators, and special tokens leave no room, the function refuses to
    construct a misleading index/query rather than losing a field silently.
    """
    special_count = int(tokenizer.num_special_tokens_to_add(pair=False))
    separator = "\n"
    label_ids = [tokenizer.encode(label + " ", add_special_tokens=False) for label, _, _ in fields]
    separator_ids = tokenizer.encode(separator, add_special_tokens=False)
    content_ids = [tokenizer.encode(value, add_special_tokens=False) for _, value, _ in fields]
    nonempty = [index for index, ids in enumerate(content_ids) if ids]
    fixed = sum(len(ids) for ids in label_ids) + max(0, len(fields) - 1) * len(separator_ids) + special_count
    available = max_seq_length - fixed
    if available < len(nonempty):
        raise ValueError("MiniLM max sequence length cannot retain every non-empty retrieval field")
    weights = [weight if content_ids[index] else 0.0 for index, (_, _, weight) in enumerate(fields)]
    total_weight = sum(weights)
    allocations = [0] * len(fields)
    for index in nonempty:
        allocations[index] = 1
    remaining = available - len(nonempty)
    # Allocate one piece at a time. A short title is capped immediately, so its
    # unused allocation flows to the longer condition/rules fields.
    while remaining > 0:
        eligible = [index for index in nonempty if allocations[index] < len(content_ids[index])]
        if not eligible:
            break
        index = max(
            eligible,
            key=lambda item: (weights[item] / (allocations[item] + 1), len(content_ids[item]) - allocations[item], -item),
        )
        allocations[index] += 1
        remaining -= 1

    def render() -> tuple[str, list[int]]:
        rendered = []
        for index, (label, _, _) in enumerate(fields):
            text = tokenizer.decode(content_ids[index][: allocations[index]], skip_special_tokens=True, clean_up_tokenization_spaces=False)
            rendered.append(label + " " + text)
        value = "\n".join(rendered)
        return value, tokenizer.encode(value, add_special_tokens=True, truncation=False)

    text, final_ids = render()
    while len(final_ids) > max_seq_length:
        reducible = [index for index in nonempty if allocations[index] > 1]
        if not reducible:
            raise ValueError("Tokenizer re-encoding exceeded max sequence length before all fields fit")
        index = max(reducible, key=lambda item: (allocations[item], weights[item], -item))
        allocations[index] -= 1
        text, final_ids = render()
    field_records = []
    for index, (label, _, weight) in enumerate(fields):
        field_records.append(
            {
                "label": label,
                "weight": weight,
                "source_token_count": len(content_ids[index]),
                "retained_token_count": allocations[index],
                "retained_token_ids": content_ids[index][: allocations[index]],
                "present": not content_ids[index] or allocations[index] > 0,
            }
        )
    if not all(record["present"] for record in field_records):
        raise ValueError("A non-empty retrieval field would be absent after token allocation")
    return {
        "text": text,
        "token_ids": final_ids,
        "token_count": len(final_ids),
        "max_seq_length": max_seq_length,
        "special_token_count": special_count,
        "fields": field_records,
    }


def skill_index_record(tokenizer: Any, skill: dict[str, Any], max_seq_length: int) -> dict[str, Any]:
    return token_budgeted_text(
        tokenizer,
        [
            ("Title:", str(skill.get("title", "")), 0.15),
            ("When to apply:", str(skill.get("when_to_apply", "")), 0.35),
            ("Rules:", " ".join(skill.get("rules", [])), 0.50),
        ],
        max_seq_length,
    )


def description_index_record(tokenizer: Any, description: dict[str, Any], max_seq_length: int) -> dict[str, Any]:
    """Encode D01 fields with the same real-token safeguards used for skills."""
    return token_budgeted_text(
        tokenizer,
        [
            ("Task family:", str(description.get("task_family", "")), 0.20),
            ("Observed obstacle:", str(description.get("observed_obstacle", "")), 0.30),
            ("Attempted procedure:", str(description.get("attempted_procedure", "")), 0.35),
            ("Observed outcome:", str(description.get("observed_outcome", "")), 0.15),
        ],
        max_seq_length,
    )


def query_record(tokenizer: Any, query_type: str, fields: dict[str, str], max_seq_length: int) -> dict[str, Any]:
    if query_type == "task":
        shape = [("Goal/problem:", fields.get("goal_problem", ""), 0.70), ("Repo/benchmark context:", fields.get("repo_context", ""), 0.30)]
    elif query_type == "event":
        shape = [
            ("Observation/errors/tests:", fields.get("observation_errors_tests", ""), 0.45),
            ("Recent action:", fields.get("recent_action", ""), 0.20),
            ("Public reasoning:", fields.get("public_reasoning", ""), 0.20),
            ("Task context:", fields.get("task_context", ""), 0.15),
        ]
    else:
        raise ValueError("query_type must be task or event")
    record = token_budgeted_text(tokenizer, shape, max_seq_length)
    record["query_type"] = query_type
    return record


@dataclass
class MiniLMEncoder:
    repo_id: str = "sentence-transformers/all-MiniLM-L6-v2"
    revision: str | None = None
    normalize_embeddings: bool = True
    _model: Any | None = None
    resolved_revision: str | None = None

    def load(self) -> dict[str, Any]:
        from huggingface_hub import model_info
        from sentence_transformers import SentenceTransformer

        if self.revision is None:
            self.resolved_revision = model_info(self.repo_id).sha
        else:
            self.resolved_revision = self.revision
        self._model = SentenceTransformer(self.repo_id, revision=self.resolved_revision)
        tokenizer = self._model.tokenizer
        return {
            "repo_id": self.repo_id,
            "resolved_revision": self.resolved_revision,
            "sentence_transformers_version": __import__("sentence_transformers").__version__,
            "max_seq_length": self._model.max_seq_length,
            "tokenizer_class": tokenizer.__class__.__name__,
            "tokenizer_vocab_size": getattr(tokenizer, "vocab_size", None),
            "normalize_embeddings": self.normalize_embeddings,
        }

    def encode(self, values: list[str]) -> list[list[float]]:
        if self._model is None:
            raise RuntimeError("MiniLMEncoder.load() must succeed before encode()")
        vectors = self._model.encode(values, normalize_embeddings=self.normalize_embeddings, convert_to_numpy=True)
        return [vector.astype(float).tolist() for vector in vectors]

    def index_skill(self, skill: dict[str, Any]) -> tuple[list[float], dict[str, Any]]:
        if self._model is None:
            raise RuntimeError("MiniLMEncoder.load() must succeed before index_skill()")
        record = skill_index_record(self._model.tokenizer, skill, self._model.max_seq_length)
        return self.encode([record["text"]])[0], record

    def index_description(self, description: dict[str, Any]) -> tuple[list[float], dict[str, Any]]:
        if self._model is None:
            raise RuntimeError("MiniLMEncoder.load() must succeed before index_description()")
        record = description_index_record(self._model.tokenizer, description, self._model.max_seq_length)
        return self.encode([record["text"]])[0], record

    def encode_query(self, query_type: str, fields: dict[str, str]) -> tuple[list[float], dict[str, Any]]:
        if self._model is None:
            raise RuntimeError("MiniLMEncoder.load() must succeed before encode_query()")
        record = query_record(self._model.tokenizer, query_type, fields, self._model.max_seq_length)
        return self.encode([record["text"]])[0], record


def rank(query_vector: list[float], skills: list[dict[str, Any]], vectors: list[list[float]], threshold: float, limit: int) -> list[dict[str, Any]]:
    scored = []
    for skill, vector in zip(skills, vectors, strict=True):
        score = cosine(query_vector, vector)
        if score >= threshold:
            scored.append({"skill": skill, "score": score, "index_text": skill_index_text(skill)})
    return sorted(scored, key=lambda item: (-item["score"], item["skill"]["skill_id"]))[:limit]
