"""Matching declarations across engines and the golden.

The three vocabularies share no identity scheme, so declarations are matched on
`(path, kind_class, name)` and collisions — overloads, same-named nested types —
are resolved by assigning the closest start lines within the colliding group.

The line number is deliberately NOT part of the key. If it were, a single
systematic off-by-one in one engine's span convention would fail every match and
be reported as ~0% accuracy. Assignment degrades gracefully and, more usefully,
*measures* the offset instead of assuming it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from benchmark.model import Decl


@dataclass
class JoinResult:
    matched: list[tuple[Decl, Decl]] = field(default_factory=list)
    only_left: list[Decl] = field(default_factory=list)
    only_right: list[Decl] = field(default_factory=list)

    def line_deltas(self) -> list[int]:
        return [
            abs(left.start_line - right.start_line)
            for left, right in self.matched
            if left.start_line is not None and right.start_line is not None
        ]

    def exact_line_rate(self) -> float:
        deltas = self.line_deltas()
        if not deltas:
            return 1.0
        return sum(1 for d in deltas if d == 0) / len(deltas)


def join(left: list[Decl], right: list[Decl], *, tolerance: int = 2) -> JoinResult:
    """Match two declaration sets.

    ``left`` is conventionally the reference (the golden), ``right`` the engine.
    """
    buckets_left: dict[tuple, list[Decl]] = {}
    buckets_right: dict[tuple, list[Decl]] = {}
    for decl in left:
        buckets_left.setdefault(decl.join_key, []).append(decl)
    for decl in right:
        buckets_right.setdefault(decl.join_key, []).append(decl)

    result = JoinResult()

    for key, group_left in buckets_left.items():
        group_right = buckets_right.pop(key, [])
        if not group_right:
            result.only_left.extend(group_left)
            continue

        if len(group_left) == 1 and len(group_right) == 1:
            result.matched.append((group_left[0], group_right[0]))
            continue

        matched_left, matched_right = _assign(group_left, group_right, tolerance)
        result.matched.extend(matched_left)
        result.only_left.extend(d for d in group_left if d not in {m[0] for m in matched_left})
        result.only_right.extend(matched_right)

    for leftovers in buckets_right.values():
        result.only_right.extend(leftovers)

    return result


def _assign(
    group_left: list[Decl], group_right: list[Decl], tolerance: int
) -> tuple[list[tuple[Decl, Decl]], list[Decl]]:
    """Greedy closest-line assignment within one colliding group.

    Groups here are overload sets — two or three members — so a greedy pass over
    the sorted candidate pairs gives the same answer as an optimal assignment at a
    fraction of the complexity.
    """
    candidates = []
    for i, a in enumerate(group_left):
        for j, b in enumerate(group_right):
            if a.start_line is None or b.start_line is None:
                cost = tolerance + 1
            else:
                cost = abs(a.start_line - b.start_line)
            if cost <= tolerance:
                candidates.append((cost, i, j))
    candidates.sort()

    used_left: set[int] = set()
    used_right: set[int] = set()
    pairs: list[tuple[Decl, Decl]] = []
    for cost, i, j in candidates:
        if i in used_left or j in used_right:
            continue
        used_left.add(i)
        used_right.add(j)
        pairs.append((group_left[i], group_right[j]))

    unmatched_right = [b for j, b in enumerate(group_right) if j not in used_right]
    return pairs, unmatched_right
