"""Rule-based identifier lint for extracted skills (docs/DECISIONS.md P1).

Figs. 6, 7 and 9 forbid repository names, exact paths, code symbols and one-off
literals; the model does not reliably comply, so findings trigger one revision
call and a skill that still fails is dropped.  Standard commands and tool names
are allowed.
"""

from __future__ import annotations

import builtins
import re
from typing import Any

# First words of a backtick span that name a standard command or tool, not a task-specific symbol.
STANDARD_COMMANDS = frozenset(
    """apt apt-get awk bash cargo cat cc cd chmod cmake cp curl diff dpkg echo env export find gcc git go
    grep gunzip gzip head hexdump iconv javac java kill less ls make mkdir mv node npm od openssl patch perl
    pip pip3 pkg-config printf ps pytest python python3 rm rustc sed sh sort ssh strace strings tail tar
    tee timeout touch tox tr uniq unzip wc wget which xargs xxd xz yarn""".split()
)

PATH = re.compile(r"(?<![\w.])(?:/[\w.-]+){2,}/?|\b[\w-]+(?:/[\w.-]+)+\.[A-Za-z]{1,5}\b")
COMMIT = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
BACKTICK = re.compile(r"`([^`]+)`")
CODE_SYMBOL = re.compile(r"\w[._]\w|\w\(|\b[a-z]+[A-Z]\w*|\b[A-Z][a-z]+[A-Z]\w*")
# Quotes are consumed in left-to-right pairs; single quotes are ambiguous with apostrophes.
QUOTED = re.compile(r'"([^"\n]*)"')
GENERIC_PATH = re.compile(r"/(?:proc|sys|dev)/")  # kernel and device interfaces are standard, not task paths
ENV_OR_FLAG = re.compile(r"[A-Z][A-Z0-9_]*|-[\w-]+")
LONG_NUMBER = re.compile(r"\b\d{5,}\b")
# Outside backticks the model often writes code names bare: snake_case, dunders, and calls.
# "attribute(s)" is English, not a call.
BARE_SYMBOL = re.compile(r"(?<!\w)__\w+__|(?<![`\w./-])(?:\w+(?:\.\w+)*\((?!s\))|[A-Za-z]\w*_\w+)")
PYTHON_BUILTINS = frozenset(name for name in dir(builtins) if not name.startswith("_"))


def _span_is_code_symbol(span: str) -> bool:
    words = span.split()
    if words and words[0] in STANDARD_COMMANDS or ENV_OR_FLAG.fullmatch(span):
        return False
    return bool(CODE_SYMBOL.search(span))


def _bare_is_code_symbol(token: str) -> bool:
    if ENV_OR_FLAG.fullmatch(token):
        return False
    name = token.rstrip("(")
    return name not in STANDARD_COMMANDS and not (token.endswith("(") and name in PYTHON_BUILTINS)


def lint_skill(skill: dict[str, Any], repo_terms: frozenset[str] = frozenset()) -> list[dict[str, str]]:
    """Return one finding per offending string in the title, when_to_apply, or rules."""
    fields = [("title", skill["title"]), ("when_to_apply", skill["when_to_apply"])]
    fields += [(f"rules[{i}]", rule) for i, rule in enumerate(skill["rules"])]
    findings = []
    for name, text in fields:
        hits = [("path", m.group()) for m in PATH.finditer(text) if not GENERIC_PATH.match(m.group())]
        hits += [("commit_hash", m.group()) for m in COMMIT.finditer(text)]
        hits += [("code_symbol", m.group(1)) for m in BACKTICK.finditer(text) if _span_is_code_symbol(m.group(1))]
        bare = BACKTICK.sub(" ", text)
        hits += [("code_symbol", m.group()) for m in BARE_SYMBOL.finditer(bare) if _bare_is_code_symbol(m.group())]
        hits += [("long_literal", m.group(1)) for m in QUOTED.finditer(text) if len(m.group(1)) >= 20]
        hits += [("long_literal", m.group()) for m in LONG_NUMBER.finditer(text)]
        lowered = text.lower()
        hits += [("repo_name", term) for term in sorted(repo_terms) if re.search(rf"\b{re.escape(term)}\b", lowered)]
        findings += [{"field": name, "kind": kind, "text": value} for kind, value in hits]
    return findings


def repo_terms_for(instance_ids: list[str]) -> frozenset[str]:
    """Owner and repository names from SWE-style instance ids (owner__repo-123)."""
    terms = set()
    for instance_id in instance_ids:
        match = re.fullmatch(r"([\w.-]+)__([\w.-]+)-\d+", instance_id)
        if match:
            terms.update(part.lower() for part in match.groups())
    return frozenset(terms)
