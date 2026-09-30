"""Loader for the hand-written ground truth, and the checks that keep it honest.

The golden file is the reference both engines are scored against, so an error in
it silently becomes an error in every number downstream. These checks exist to
make that impossible rather than unlikely:

* every `evidence.quote` is re-read from the source at the recorded line, so a
  wrong line number or a drifted fixture fails the run instead of quietly
  mis-scoring an engine;
* `counts` must equal the number of rows, so a fact cannot be deleted because it
  was inconvenient;
* every id referenced by a call, hierarchy or member-map row must resolve to a
  declared entity, so the graph of facts is internally closed.

None of this can prove the golden is *complete* — only that what it claims is
真. Completeness is what `text_scan.py` independently cross-checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SECTIONS = (
    "declarations",
    "hierarchy",
    "member_map",
    "calls",
    "creations",
    "di",
    "projects",
    "expected_absent",
)


class GoldenError(Exception):
    """The golden file contradicts the source, or itself."""


@dataclass
class Golden:
    path: Path
    root: Path
    raw: dict[str, Any]
    problems: list[str] = field(default_factory=list)

    def __getattr__(self, name: str) -> list[dict[str, Any]]:
        if name in SECTIONS:
            return self.raw.get(name, [])
        raise AttributeError(name)

    def declaration(self, identifier: str) -> dict[str, Any] | None:
        return self._by_id.get(identifier)

    @property
    def _by_id(self) -> dict[str, dict[str, Any]]:
        return {d["id"]: d for d in self.raw.get("declarations", [])}

    def internal_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["target"] == "internal"]

    def external_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["target"] == "external"]

    def unresolvable_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["target"] == "unresolvable"]

    def bare_call_share(self) -> float:
        """Share of calls written without a receiver.

        A receiver-text matcher cannot see these at all, so this is the disclosed
        ceiling on such an engine's call recall — published so its score is read
        against the right maximum.
        """
        if not self.calls:
            return 0.0
        return sum(1 for c in self.calls if c.get("receiver") == "bare") / len(self.calls)

    def overloaded_members(self) -> set[tuple[str, str]]:
        """(owner, name) pairs declared more than once — the overload sets."""
        seen: dict[tuple[str, str], int] = {}
        for declaration in self.declarations:
            if declaration["kind"] not in {"Method", "Constructor"}:
                continue
            key = (declaration.get("owner", ""), declaration["name"])
            seen[key] = seen.get(key, 0) + 1
        return {key for key, count in seen.items() if count > 1}


def load(path: Path | str, *, strict: bool = True) -> Golden:
    """Load and verify the golden file.

    With ``strict``, any problem raises. The benchmark runs strict; the authoring
    workflow sets it false to see every problem at once instead of the first.
    """
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    root = (path.parent.parent.parent / raw["root"]).resolve() if "root" in raw else path.parent
    # `root` in the file is repo-relative; resolve it against the repository root.
    repo_root = Path(__file__).resolve().parent.parent.parent
    root = (repo_root / raw["root"]).resolve()

    golden = Golden(path=path, root=root, raw=raw)
    golden.problems = _verify(golden)

    if strict and golden.problems:
        listed = "\n  ".join(golden.problems[:25])
        more = f"\n  ... and {len(golden.problems) - 25} more" if len(golden.problems) > 25 else ""
        raise GoldenError(
            f"{len(golden.problems)} problem(s) in {path}:\n  {listed}{more}"
        )
    return golden


def _verify(golden: Golden) -> list[str]:
    problems: list[str] = []
    problems += _verify_counts(golden)
    problems += _verify_unique_ids(golden)
    problems += _verify_evidence(golden)
    problems += _verify_references(golden)
    return problems


def _verify_counts(golden: Golden) -> list[str]:
    declared = golden.raw.get("counts")
    if not declared:
        return ["no `counts` block: a deleted row would go unnoticed"]

    problems = []
    for section, expected in declared.items():
        actual = len(golden.raw.get(section, []))
        if actual != expected:
            problems.append(f"counts.{section} says {expected} but there are {actual} rows")
    return problems


def _verify_unique_ids(golden: Golden) -> list[str]:
    problems = []
    for section in ("declarations", "calls", "creations"):
        seen: set[str] = set()
        for row in golden.raw.get(section, []):
            identifier = row.get("id")
            if identifier is None:
                continue
            if identifier in seen:
                problems.append(f"{section}: duplicate id {identifier!r}")
            seen.add(identifier)
    return problems


def _verify_evidence(golden: Golden) -> list[str]:
    """Re-read every quoted line from the source.

    This is the check that makes the golden trustworthy: it cannot drift away from
    the code without the run failing.
    """
    problems: list[str] = []
    cache: dict[str, list[str]] = {}

    def lines_of(relative: str) -> list[str] | None:
        if relative not in cache:
            source = golden.root / relative
            if not source.is_file():
                return None
            cache[relative] = source.read_text().splitlines()
        return cache[relative]

    for section in ("declarations", "calls"):
        for row in golden.raw.get(section, []):
            evidence = row.get("evidence")
            if not evidence:
                problems.append(f"{section} {row.get('id')}: no evidence")
                continue

            relative = row["path"]
            source_lines = lines_of(relative)
            if source_lines is None:
                problems.append(f"{section} {row.get('id')}: {relative} does not exist")
                continue

            # `line` on a call is the call site; on a declaration the evidence may
            # name its own line, defaulting to the declaration's start.
            line_number = evidence.get("line", row.get("line", row.get("start_line")))
            if not isinstance(line_number, int) or not 1 <= line_number <= len(source_lines):
                problems.append(
                    f"{section} {row.get('id')}: line {line_number} outside {relative} "
                    f"(1..{len(source_lines)})"
                )
                continue

            actual = source_lines[line_number - 1].strip()
            quote = evidence["quote"].strip()
            if quote not in actual:
                problems.append(
                    f"{section} {row.get('id')}: {relative}:{line_number} is\n"
                    f"      actual: {actual!r}\n"
                    f"      quoted: {quote!r}"
                )

    return problems


def _verify_references(golden: Golden) -> list[str]:
    """Every id a fact points at must be declared."""
    known = {d["id"] for d in golden.declarations}
    problems: list[str] = []

    def check(identifier: str | None, where: str) -> None:
        if identifier is not None and identifier not in known:
            problems.append(f"{where}: unknown id {identifier!r}")

    for declaration in golden.declarations:
        check(declaration.get("owner"), f"declarations {declaration['id']}.owner")

    for row in golden.hierarchy:
        check(row["sub"], "hierarchy.sub")
        check(row["super"], "hierarchy.super")

    for row in golden.member_map:
        check(row["impl"], "member_map.impl")
        check(row["target"], "member_map.target")

    for row in golden.calls:
        check(row["caller"], f"calls {row['id']}.caller")
        if row["target"] == "internal":
            check(row.get("callee"), f"calls {row['id']}.callee")
        elif row["target"] == "external" and not row.get("callee_external"):
            problems.append(f"calls {row['id']}: external but no callee_external")

    for row in golden.creations:
        check(row["caller"], f"creations {row['id']}.caller")
        if "type" in row:
            check(row["type"], f"creations {row['id']}.type")
        elif "type_external" not in row:
            problems.append(f"creations {row['id']}: neither type nor type_external")

    for row in golden.expected_absent:
        check(row.get("subject"), "expected_absent.subject")
        # `object` may name an external symbol, so it is not required to resolve.

    return problems
