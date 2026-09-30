"""Declaration accuracy, with capability gaps held strictly apart from errors.

A kind an engine does not model at all is not a wrong answer — it is an absent
feature. Scoring it as a miss would turn "tree-sitter does not model fields" into
"tree-sitter got the fields wrong", which is a different and unfair claim. So
unmodelled kinds are removed from that engine's denominator and reported in the
capability table instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from benchmark.metrics.sets import Score, score
from benchmark.model import Decl, KindClass
from benchmark.normalize.decls import canonical, implied_namespaces, namespace_names
from benchmark.normalize.join import JoinResult, join

#: What each engine models AT ALL, read off its source vocabulary rather than its
#: output — an engine that models a kind but found none of them has a bug, while
#: one that models none has a gap, and the two must not look alike.
MODELED: dict[str, set[KindClass]] = {
    # app/contract/models.py EntityKind
    "roslyn": {
        KindClass.NAMESPACE, KindClass.TYPE, KindClass.DELEGATE, KindClass.CALLABLE,
        KindClass.PROPERTY, KindClass.FIELD, KindClass.EVENT, KindClass.FILE,
        KindClass.EXTERNAL,
    },
    # code_analysis.py DECLARATION_TYPES["csharp"] — no field_declaration,
    # event_declaration, delegate_declaration, operator or destructor.
    "tree-sitter": {
        KindClass.NAMESPACE, KindClass.TYPE, KindClass.CALLABLE, KindClass.PROPERTY,
        KindClass.FILE, KindClass.EXTERNAL,
    },
}

#: Kinds worth scoring by the path-based join. Namespaces are excluded and
#: compared as name sets instead: Roslyn emits one global node per namespace and
#: tree-sitter one per file, so joining them by path would score a representation
#: difference as an accuracy failure.
SCORED = {
    KindClass.TYPE, KindClass.DELEGATE, KindClass.CALLABLE,
    KindClass.PROPERTY, KindClass.FIELD, KindClass.EVENT,
}


@dataclass
class DeclReport:
    engine: str
    overall: Score
    per_kind: dict[KindClass, Score]
    join_result: JoinResult
    unmodelled: dict[KindClass, int]
    owner_matches: int
    owner_comparable: int
    namespaces: Score
    implied_namespaces: set[str]
    multi_node_types: dict[str, int]

    @property
    def owner_match_rate(self) -> float:
        return self.owner_matches / self.owner_comparable if self.owner_comparable else 0.0


def _strip_generics(name: str) -> str:
    """`Repository<T>` and `Repository` name the same type.

    Roslyn renders type parameters, the golden does not. That is a formatting
    difference, and scoring it as a wrong owner would misreport it as an error.
    """
    out, depth = [], 0
    for ch in name:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _same_owner(left: str, right: str) -> bool:
    return _strip_generics(left) == _strip_generics(right)


def _dedupe_types(decls: list[Decl]) -> list[Decl]:
    """Collapse each type to a single entity, keeping its earliest declaration.

    A partial type is ONE type declared in several files. Roslyn models it that
    way; tree-sitter emits a node per file. Counting nodes would charge one of them
    for a representation choice, so both are reduced to the type itself and how
    each represents it is reported separately.
    """
    seen: dict[tuple, Decl] = {}
    output: list[Decl] = []
    for decl in decls:
        if decl.kind_class is not KindClass.TYPE:
            output.append(decl)
            continue
        key = decl.join_key
        existing = seen.get(key)
        if existing is None:
            seen[key] = decl
            output.append(decl)
        elif (decl.start_line or 0) < (existing.start_line or 0):
            output[output.index(existing)] = decl
            seen[key] = decl
    return output


def partial_type_sites(decls: list[Decl]) -> dict[str, int]:
    """How many nodes each type is represented by, for types with more than one."""
    counts: dict[str, int] = {}
    for decl in decls:
        if decl.kind_class is KindClass.TYPE:
            name = f"{decl.owner or ''}.{decl.name}"
            counts[name] = counts.get(name, 0) + 1
    return {name: count for name, count in counts.items() if count > 1}


def evaluate(golden_decls: list[Decl], engine_graph, tolerance: int = 2) -> DeclReport:
    engine = engine_graph.engine
    modelled = MODELED[engine]

    golden_canonical = canonical(golden_decls)
    engine_canonical = canonical(engine_graph.decls)

    reference = _dedupe_types([d for d in golden_canonical if d.kind_class in SCORED])
    produced = _dedupe_types([
        d for d in engine_canonical
        if d.kind_class in SCORED and not d.external and not d.generated
    ])

    declared = namespace_names(golden_canonical)
    namespaces = score(
        engine=namespace_names(engine_canonical, declared),
        reference=declared,
    )
    implied = implied_namespaces(engine_canonical, declared)

    # Kinds this engine cannot represent leave the denominator entirely.
    unmodelled = {}
    for kind in SCORED - modelled:
        missing = sum(1 for d in reference if d.kind_class == kind)
        if missing:
            unmodelled[kind] = missing
    reference_scored = [d for d in reference if d.kind_class in modelled]

    result = join(reference_scored, produced, tolerance=tolerance)

    matched_keys = {id(g) for g, _ in result.matched}
    overall = score(
        engine={id(e) for _, e in result.matched} | {id(e) for e in result.only_right},
        reference=matched_keys | {id(g) for g in result.only_left},
    )
    # Recompute honestly from the join rather than from id sets.
    overall = Score(
        true_positives=len(result.matched),
        false_positives=len(result.only_right),
        false_negatives=len(result.only_left),
    )

    per_kind: dict[KindClass, Score] = {}
    for kind in sorted({d.kind_class for d in reference_scored}, key=lambda k: k.value):
        per_kind[kind] = Score(
            true_positives=sum(1 for g, _ in result.matched if g.kind_class == kind),
            false_positives=sum(1 for e in result.only_right if e.kind_class == kind),
            false_negatives=sum(1 for g in result.only_left if g.kind_class == kind),
        )

    owner_matches = owner_comparable = 0
    for reference_decl, engine_decl in result.matched:
        if reference_decl.owner is None or engine_decl.owner is None:
            continue
        owner_comparable += 1
        if _same_owner(reference_decl.owner, engine_decl.owner):
            owner_matches += 1

    return DeclReport(
        namespaces=namespaces,
        implied_namespaces=implied,
        multi_node_types=partial_type_sites(
            [d for d in engine_canonical if d.kind_class in SCORED and not d.external]),
        engine=engine,
        overall=overall,
        per_kind=per_kind,
        join_result=result,
        unmodelled=unmodelled,
        owner_matches=owner_matches,
        owner_comparable=owner_comparable,
    )
