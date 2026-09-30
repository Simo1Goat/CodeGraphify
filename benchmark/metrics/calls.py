"""Call accuracy, decomposed into levels rather than collapsed into one number.

A single call F1 would hide the thing being measured. Tree-sitter records that a
call happened and what it was written as, but its target is the literal receiver
text (`_repo.GetAsync`) matched against a flat index of bare names, so it can
reach the right NAME while never reaching a declaration. Reporting one figure
would either flatter it (crediting the name) or bury it (scoring only the
symbol). So:

  L0  caller coverage   — did the engine find that this member calls anything?
  L2  target name       — is the final identifier of the target correct?
  L3a target type+name  — does it reach the right method on the right TYPE?
  L3b target exact      — does it reach the right OVERLOAD?

`L3a - L3b` is the overload story; `L2 - L3a` is the namespace/receiver story.

Everything is de-duplicated to (caller, callee) PAIRS. Tree-sitter de-duplicates
edges on (src, dst, type) in its builder, so counting call sites would penalise it
for a modelling choice rather than an accuracy failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from benchmark.metrics.sets import Score, score


#: Target names that denote a constructor rather than a method.
CONSTRUCTOR_NAMES = {".ctor", "#ctor", "ctor", ".cctor", "#cctor"}


def last_segment(target: str | None) -> str | None:
    """The final identifier of a call target, however it was written.

    `_repo.QueryAsync<Order>()` -> `QueryAsync`
    `System.Collections.Generic.List<T>.Add` -> `Add`
    `JsonSerializer.Serialize``1` -> `Serialize`

    Generic arguments are removed by balanced scan, not by splitting at the first
    `<`: splitting truncated `List<T>.Add` to `List` and silently scored every
    generic call as a miss.
    """
    if not target:
        return None

    text = target.strip().split("(")[0]

    stripped, depth = [], 0
    for ch in text:
        if ch in "<{":
            depth += 1
        elif ch in ">}":
            depth = max(0, depth - 1)
        elif depth == 0:
            stripped.append(ch)
    text = "".join(stripped)

    # Documentation-comment arity marker: `Serialize``1` -> `Serialize`.
    if "`" in text:
        text = text.split("`")[0]

    return text.rstrip("?!").rsplit(".", 1)[-1].strip() or None


@dataclass
class CallReport:
    engine: str
    l0: Score
    l2: Score
    l3a: Score
    l3b: Score
    bare_call_share: float
    overload_total: int
    overload_correct: int
    overload_chance: float
    confident_total: int
    confident_correct: int

    @property
    def overload_rate(self) -> float:
        return self.overload_correct / self.overload_total if self.overload_total else 0.0

    @property
    def precision_confident(self) -> float:
        return self.confident_correct / self.confident_total if self.confident_total else 0.0

    def check_monotonic(self) -> None:
        """TP must shrink as the levels get stricter.

        A violation is a normalization bug, not a finding, so it is asserted rather
        than reported.
        """
        assert self.l3b.true_positives <= self.l3a.true_positives <= self.l2.true_positives, (
            f"{self.engine}: call levels not monotonic "
            f"(L3b {self.l3b.true_positives} L3a {self.l3a.true_positives} L2 {self.l2.true_positives})"
        )


def _match(engine_units: list, reference_units: list, key) -> Score:
    """One-to-one matching at a given level of strictness.

    Not a set intersection on the level's key: keying on less information merges
    distinct facts (two overloads collapse to one entry), which made the relaxed
    level report FEWER true positives than the strict one. Matching keeps the unit
    count fixed, so the levels nest the way they are meant to.
    """
    buckets: dict = {}
    for unit in reference_units:
        buckets.setdefault(key(unit), []).append(unit)

    true_positives = 0
    false_positives = 0
    for unit in engine_units:
        bucket = buckets.get(key(unit))
        if bucket:
            bucket.pop()
            true_positives += 1
        else:
            false_positives += 1

    false_negatives = sum(len(v) for v in buckets.values())
    return Score(true_positives, false_positives, false_negatives)


@dataclass(frozen=True)
class CallUnit:
    """One (caller, callee) pair, de-duplicated.

    Pairs rather than call sites: tree-sitter de-duplicates edges on
    (src, dst, type) in its builder, so counting sites would charge it for a
    modelling choice rather than an accuracy failure.
    """

    caller: str
    callee_id: str | None      # golden id when the target is an in-corpus declaration
    callee_owner: str | None
    callee_name: str | None
    internal: bool


def golden_units(golden) -> list[CallUnit]:
    by_id = {d["id"]: d for d in golden.declarations}
    units: set[CallUnit] = set()
    for call in golden.calls:
        caller = call["caller"]
        if call["target"] == "internal":
            callee = by_id[call["callee"]]
            units.add(CallUnit(caller, call["callee"], callee.get("owner"),
                               callee["name"].rsplit(".", 1)[-1], True))
        elif call["target"] == "external":
            units.add(CallUnit(caller, None, None,
                               last_segment(call["callee_external"]), False))
        else:
            # `unresolvable`: contributes a caller but no target. Naming a target
            # here is fabrication, which `expected_absent` checks separately.
            units.add(CallUnit(caller, None, None, None, False))
    return sorted(units, key=lambda u: (u.caller, u.callee_id or "", u.callee_name or ""))


def engine_units(graph, uid_to_golden: dict[str, str], golden) -> list[CallUnit]:
    by_id = {d["id"]: d for d in golden.declarations}
    units: set[CallUnit] = set()

    decl_by_uid = {d.raw.get("uid"): d for d in graph.decls if d.raw.get("uid")}

    for edge in graph.edges:
        if edge.edge_class.value != "Call":
            continue
        caller = uid_to_golden.get(edge.src_uid)
        if caller is None:
            continue

        # Object creation is scored under `creations`, not as a call: several types
        # here have an IMPLICIT constructor that no source line declares, so
        # scoring `new X()` as a call to an undeclared member would penalise
        # whichever engine happens not to synthesize one. The golden excludes them,
        # so the engine side must too.
        target_decl = decl_by_uid.get(edge.dst_uid) if edge.dst_uid else None
        raw_name = edge.dst_external or edge.dst_name or ""
        if (target_decl is not None and target_decl.kind_raw == "Constructor") or \
                last_segment(raw_name) in CONSTRUCTOR_NAMES or \
                raw_name.rsplit(".", 1)[-1] in CONSTRUCTOR_NAMES:
            continue

        callee_golden = uid_to_golden.get(edge.dst_uid) if edge.dst_uid else None
        if callee_golden and callee_golden in by_id:
            callee = by_id[callee_golden]
            units.add(CallUnit(caller, callee_golden, callee.get("owner"),
                               callee["name"].rsplit(".", 1)[-1], True))
        else:
            name = last_segment(edge.dst_external) or last_segment(edge.dst_name)
            units.add(CallUnit(caller, None, None, name, False))

    return sorted(units, key=lambda u: (u.caller, u.callee_id or "", u.callee_name or ""))


def evaluate(graph, uid_to_golden: dict[str, str], golden) -> CallReport:
    reference = golden_units(golden)
    produced = engine_units(graph, uid_to_golden, golden)

    internal_reference = [u for u in reference if u.internal]
    internal_produced = [u for u in produced if u.internal]

    l0 = _match(
        [CallUnit(c, None, None, None, False) for c in sorted({u.caller for u in produced})],
        [CallUnit(c, None, None, None, False) for c in sorted({u.caller for u in reference})],
        key=lambda u: u.caller,
    )
    # L2 includes external targets: getting the NAME right is the most an engine
    # without symbol resolution can do, and it should get credit for it.
    named_produced = [u for u in produced if u.callee_name]
    named_reference = [u for u in reference if u.callee_name]
    l2 = _match(named_produced, named_reference, key=lambda u: (u.caller, u.callee_name))
    l3a = _match(internal_produced, internal_reference,
                 key=lambda u: (u.caller, u.callee_owner, u.callee_name))
    l3b = _match(internal_produced, internal_reference,
                 key=lambda u: (u.caller, u.callee_id))

    # Overload discrimination, restricted to calls whose true target is overloaded.
    overloaded = golden.overloaded_members()
    by_id = {d["id"]: d for d in golden.declarations}
    overload_calls = []
    for call in golden.calls:
        if call["target"] != "internal":
            continue
        callee = by_id[call["callee"]]
        key = (callee.get("owner", ""), callee["name"])
        if key in overloaded:
            overload_calls.append(call)

    exact_pairs = {(u.caller, u.callee_id) for u in internal_produced}
    overload_correct = sum(
        1 for c in overload_calls if (c["caller"], c["callee"]) in exact_pairs
    )
    # Random-pick baseline: without signature information, an engine choosing
    # among an overload set gets 1/k. Reporting the rate without this makes a
    # chance-level score look like a finding.
    chance = 0.0
    if overload_calls:
        sizes = []
        for call in overload_calls:
            callee = by_id[call["callee"]]
            key = (callee.get("owner", ""), callee["name"])
            size = sum(
                1 for d in golden.declarations
                if (d.get("owner", ""), d["name"]) == key
            )
            sizes.append(1 / size if size else 0.0)
        chance = sum(sizes) / len(sizes)

    # Calibration: of the calls an engine claims to have RESOLVED to an in-corpus
    # declaration, how many are right? Restricted to those claims deliberately —
    # an edge to an external symbol is confident but has no in-corpus target to be
    # graded against, and counting it as wrong would understate both engines.
    reference_pairs = {(u.caller, u.callee_id) for u in internal_reference}
    confident_claims = {
        (u.caller, u.callee_id) for u in internal_produced if u.callee_id is not None
    }
    confident_total = len(confident_claims)
    confident_correct = len(confident_claims & reference_pairs)

    report = CallReport(
        engine=graph.engine,
        l0=l0,
        l2=l2,
        l3a=l3a,
        l3b=l3b,
        bare_call_share=golden.bare_call_share(),
        overload_total=len(overload_calls),
        overload_correct=overload_correct,
        overload_chance=chance,
        confident_total=confident_total,
        confident_correct=confident_correct,
    )
    report.check_monotonic()
    return report
