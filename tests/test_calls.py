"""Stage 3: the call graph.

This is the stage where a wrong implementation still produces a plausible-looking
graph, so these tests are mostly about the cases that quietly go wrong: overloads
merging, `base.` calls recorded as virtual, generic instantiations fragmenting the
graph, cross-project calls looking external, and uncertainty being papered over.
"""

from __future__ import annotations

import pytest

from app.extractor_runner import ExtractionResult
from tests.conftest import read_jsonl, requires_dotnet, requires_extractor

pytestmark = [requires_extractor, requires_dotnet]

CALL_KINDS = {"CALLS_DIRECT", "CALLS_VIRTUAL", "CALLS_INTERFACE", "CALLS_UNRESOLVED"}


# --- helpers ------------------------------------------------------------------


def _by_name(entities: dict[str, dict], name: str, kind: str | None = None) -> list[dict]:
    return [
        e for e in entities.values() if e["name"] == name and (kind is None or e["kind"] == kind)
    ]


def _member(entities: dict[str, dict], declaring_type: str, name: str) -> dict:
    matches = [
        e
        for e in entities.values()
        if e["name"] == name and e["metadata"].get("declaring_type") == declaring_type
    ]
    assert len(matches) == 1, f"expected one {declaring_type}.{name}, got {len(matches)}"
    return matches[0]


def _outgoing(
    relations: list[dict], source_id: str, kinds: set[str] | None = None
) -> list[dict]:
    return [
        r
        for r in relations
        if r["source"] == source_id and (kinds is None or r["kind"] in kinds)
    ]


def _edge_at(relations: list[dict], source_id: str, line: int) -> list[dict]:
    return [r for r in _outgoing(relations, source_id) if r["evidence"]["start_line"] == line]


@pytest.fixture(scope="module")
def run_all(entities: dict[str, dict]) -> dict:
    return _member(entities, "Acme.App.OrderRunner", "RunAll")


# --- graph integrity ----------------------------------------------------------


def test_no_call_edge_dangles(entities: dict[str, dict], relations: list[dict]) -> None:
    """A dangling edge makes Neo4j MERGE a blank node, silently corrupting the
    graph. Synthesized members (a delegate's Invoke, an implicit constructor) are
    the easy way to get one, since stage 2 does not emit them."""
    dangling = [
        r
        for r in relations
        if r["kind"] in CALL_KINDS | {"CREATES", "RESOLVES_TO"}
        and (r["source"] not in entities or r["target"] not in entities)
    ]
    assert dangling == []


def test_every_call_edge_carries_source_evidence(relations: list[dict]) -> None:
    """Context section 4: an edge without a location cannot be verified against
    the code, and a graph nobody can check is not worth trusting."""
    for relation in relations:
        if relation["kind"] not in CALL_KINDS | {"CREATES", "RESOLVES_TO"}:
            continue
        evidence = relation["evidence"]
        assert evidence is not None, relation["kind"]
        assert evidence["file_path"]
        assert evidence["start_line"] >= 1
        assert evidence["start_column"] >= 1
        assert evidence["end_line"] >= evidence["start_line"]


def test_every_call_edge_records_dispatch_and_resolution_separately(
    relations: list[dict],
) -> None:
    """Two different questions: how the call reaches its target, and how
    confidently the target was identified. A virtual call into a referenced
    assembly is External + Virtual, and one field cannot hold both."""
    for relation in relations:
        if relation["kind"] not in CALL_KINDS:
            continue
        metadata = relation["metadata"]
        assert metadata["dispatch_kind"] in {
            "Direct",
            "Virtual",
            "Interface",
            "Dynamic",
            "Reflection",
            "None",
        }
        assert metadata["resolution_status"] in {
            "Resolved",
            "External",
            "VirtualDispatch",
            "InterfaceDispatch",
            "Dynamic",
            "ReflectionBased",
            "Ambiguous",
            "Unresolved",
            "ParseError",
        }


# --- dispatch classification --------------------------------------------------


def test_interface_call_is_interface_dispatch(
    entities: dict[str, dict], relations: list[dict], run_all: dict
) -> None:
    edges = [
        r
        for r in _outgoing(relations, run_all["id"], {"CALLS_INTERFACE"})
        if entities[r["target"]]["name"] == "Submit"
    ]
    assert len(edges) == 2
    for edge in edges:
        assert edge["metadata"]["dispatch_kind"] == "Interface"
        assert edge["metadata"]["resolution_status"] == "InterfaceDispatch"


def test_overloads_resolve_to_different_targets(
    entities: dict[str, dict], relations: list[dict], run_all: dict
) -> None:
    """The single clearest reason this needs semantic analysis: Submit(order) and
    Submit(order, force: true) differ only in arguments, and a syntax-only
    analyzer cannot tell which overload either one reaches."""
    submits = [
        r
        for r in _outgoing(relations, run_all["id"], {"CALLS_INTERFACE"})
        if entities[r["target"]]["name"] == "Submit"
    ]
    targets = {r["target"] for r in submits}
    assert len(targets) == 2

    signatures = {r["metadata"]["target_signature"] for r in submits}
    assert any("Order order)" in s for s in signatures)
    assert any("bool force)" in s for s in signatures)


def test_virtual_call_through_a_class_receiver_is_virtual(
    entities: dict[str, dict], relations: list[dict], run_all: dict
) -> None:
    edges = [
        r
        for r in _outgoing(relations, run_all["id"], {"CALLS_VIRTUAL"})
        if entities[r["target"]]["name"] == "Validate"
    ]
    assert len(edges) == 1
    assert edges[0]["metadata"]["resolution_status"] == "VirtualDispatch"
    assert entities[edges[0]["target"]]["metadata"]["declaring_type"] == (
        "Acme.Core.Orders.OrderServiceBase"
    )


def test_a_base_call_is_direct_not_virtual(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """`base.Validate(order)` calls a virtual method, but `base` suppresses virtual
    dispatch, so it is a direct call. Checking IsVirtual on the *symbol* would get
    this wrong; the invocation operation knows the difference."""
    submit = next(
        e
        for e in entities.values()
        if e["name"] == "Submit"
        and e["metadata"].get("declaring_type") == "Acme.Core.Orders.OrderService"
        and e["metadata"]["parameter_count"] == 1
    )
    edges = [
        r
        for r in _outgoing(relations, submit["id"])
        if entities[r["target"]]["name"] == "Validate"
    ]
    assert len(edges) == 1
    assert edges[0]["kind"] == "CALLS_DIRECT"
    assert edges[0]["metadata"]["dispatch_kind"] == "Direct"


def test_a_static_call_is_direct(
    entities: dict[str, dict], relations: list[dict], run_all: dict
) -> None:
    edges = [
        r
        for r in _outgoing(relations, run_all["id"])
        if entities[r["target"]]["name"] == "ForEach"
    ]
    assert len(edges) == 1
    assert edges[0]["kind"] == "CALLS_DIRECT"


# --- normalization ------------------------------------------------------------


def test_a_generic_call_normalizes_to_the_definition(
    entities: dict[str, dict], relations: list[dict], run_all: dict
) -> None:
    """`_repo.Add(...)` on a Repository<OrderEntity> must land on
    Repository<T>.Add. Without this every instantiation becomes its own node and
    the call graph fragments into targets nobody declared."""
    edges = [
        r for r in _outgoing(relations, run_all["id"]) if entities[r["target"]]["name"] == "Add"
    ]
    assert len(edges) == 1
    target = entities[edges[0]["target"]]
    assert target["canonical_name"] == "M:Acme.Core.Data.Repository`1.Add(`0)"
    # The constructed form is kept, so the concrete instantiation is not lost.
    assert "OrderEntity" in edges[0]["metadata"]["constructed_target"]


def test_an_extension_method_call_resolves_through_reduced_from(
    entities: dict[str, dict], relations: list[dict], run_all: dict
) -> None:
    """`_repo.AddAll(...)` is written as an instance call but declared as a static
    method taking the receiver as its first parameter."""
    edges = [
        r
        for r in _outgoing(relations, run_all["id"])
        if entities[r["target"]]["name"] == "AddAll"
    ]
    assert len(edges) == 1
    assert edges[0]["metadata"]["is_extension_method"] is True

    # The edge must land on the static declaration, with the receiver as its first
    # parameter -- not on some instance method of Repository<T>.
    target = entities[edges[0]["target"]]
    assert target["metadata"]["declaring_type"] == "Acme.Core.Data.RepositoryExtensions"
    assert target["canonical_name"].startswith(
        "M:Acme.Core.Data.RepositoryExtensions.AddAll"
    )
    assert target["metadata"]["parameter_count"] == 2


# --- cross-project vs external ------------------------------------------------


def test_a_cross_project_call_is_not_external(
    entities: dict[str, dict], relations: list[dict], run_all: dict
) -> None:
    """App calls into Core, which crosses a compilation boundary while staying
    inside the codebase. Calling that External would both mislabel it and point it
    at a duplicate node instead of Core's real declaration -- fragmenting the graph
    exactly where cross-project dependency analysis matters."""
    edges = [
        r
        for r in _outgoing(relations, run_all["id"], {"CALLS_INTERFACE"})
        if entities[r["target"]]["name"] == "Submit"
    ]
    for edge in edges:
        assert edge["metadata"]["resolution_status"] != "External"
        assert edge["metadata"]["external"] is False
        target = entities[edge["target"]]
        assert target["kind"] == "Method", "should be Core's own node, not an ExternalMethod"
        assert target["metadata"]["project_key"].endswith("Core.csproj")


def test_a_framework_call_is_external(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    submit = next(
        e
        for e in entities.values()
        if e["name"] == "Submit"
        and e["metadata"].get("declaring_type") == "Acme.Core.Orders.OrderService"
        and e["metadata"]["parameter_count"] == 1
    )
    edges = [
        r
        for r in _outgoing(relations, submit["id"])
        if entities[r["target"]]["name"] == "Add"
    ]
    assert len(edges) == 1
    assert edges[0]["metadata"]["resolution_status"] == "External"
    target = entities[edges[0]["target"]]
    assert target["kind"] == "ExternalMethod"
    assert target["metadata"]["assembly_name"]


def test_external_types_are_linked_to_their_assembly(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """So a dependency can be traced from a call out to the library it lands in."""
    external_types = [e for e in entities.values() if e["kind"] == "ExternalType"]
    assert external_types

    assemblies = {e["id"] for e in entities.values() if e["kind"] == "ExternalAssembly"}
    linked = {
        r["target"]
        for r in relations
        if r["kind"] == "CONTAINS" and r["source"] in assemblies
    }
    assert {e["id"] for e in external_types} & linked


# --- attribution --------------------------------------------------------------


def test_a_call_inside_a_lambda_is_attributed_to_the_containing_member(
    entities: dict[str, dict], relations: list[dict], run_all: dict
) -> None:
    """A lambda is not a node in phase 1, so attributing to it would leave the call
    hanging off something the graph does not contain."""
    cancels = [
        r
        for r in _outgoing(relations, run_all["id"], {"CALLS_INTERFACE"})
        if entities[r["target"]]["name"] == "Cancel"
    ]
    # One from inside the lambda, one from inside the local function.
    assert len(cancels) == 2


def test_a_call_inside_a_local_function_is_attributed_to_the_containing_member(
    entities: dict[str, dict], relations: list[dict], run_all: dict
) -> None:
    lines = {
        r["evidence"]["start_line"]
        for r in _outgoing(relations, run_all["id"], {"CALLS_INTERFACE"})
        if entities[r["target"]]["name"] == "Cancel"
    }
    # The local function body sits below the lambda; both attribute to RunAll.
    assert len(lines) == 2


def test_no_edge_points_at_a_local_function(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Local functions are not nodes, and the hop through the name carries nothing:
    calls made inside the local function are already attributed to the enclosing
    member, so dropping this edge loses no information."""
    assert [e for e in entities.values() if e["kind"] == "LocalFunction"] == []
    assert [
        r
        for r in relations
        if r["target"] in entities and entities[r["target"]]["name"] == "LocalHelper"
    ] == []


def test_a_call_from_a_property_body_is_attributed_to_the_property(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Stage 2 emits the Property, not get_Name, so the property is the caller."""
    name_property = _member(entities, "Acme.Core.PlatformInfo", "Name")
    assert name_property["kind"] == "Property"
    targets = {
        entities[r["target"]]["name"] for r in _outgoing(relations, name_property["id"])
    }
    assert "ModernOnly" in targets


# --- object creation ----------------------------------------------------------


def test_object_creation_records_both_the_type_and_the_constructor(
    entities: dict[str, dict], relations: list[dict], run_all: dict
) -> None:
    """CREATES answers "who instantiates this type", which is the dependency
    question; the constructor call is a separate fact that matters when the
    constructor has behaviour."""
    creates = [
        r
        for r in _outgoing(relations, run_all["id"], {"CREATES"})
        if entities[r["target"]]["name"] == "Order"
    ]
    assert len(creates) == 1
    assert entities[creates[0]["target"]]["kind"] == "Record"

    constructors = [
        r
        for r in _outgoing(relations, run_all["id"], {"CALLS_DIRECT"})
        if entities[r["target"]]["kind"] == "Constructor"
        and entities[r["target"]]["metadata"].get("declaring_type") == "Acme.Core.Orders.Order"
    ]
    assert len(constructors) == 1


def test_creation_in_a_field_initializer_is_attributed_to_the_field(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """`private readonly Repository<OrderEntity> _repo = new();` really does
    instantiate something, and the field is the only sensible place to hang it."""
    repo_field = _member(entities, "Acme.App.OrderRunner", "_repo")
    assert repo_field["kind"] == "Field"
    creates = _outgoing(relations, repo_field["id"], {"CREATES"})
    assert len(creates) == 1
    assert entities[creates[0]["target"]]["name"] == "Repository"


# --- method groups ------------------------------------------------------------


def test_a_method_group_is_a_reference_not_a_call(
    entities: dict[str, dict], relations: list[dict], run_all: dict
) -> None:
    """`OrderSubmitted handler = OnOrderSubmitted;` may never run. Recording it as
    a call would invent control flow that does not exist."""
    references = _outgoing(relations, run_all["id"], {"RESOLVES_TO"})
    assert len(references) == 1
    assert entities[references[0]["target"]]["name"] == "OnOrderSubmitted"
    assert references[0]["metadata"]["reference_only"] is True

    # And it must not also appear as a call.
    calls_to_handler = [
        r
        for r in _outgoing(relations, run_all["id"], CALL_KINDS)
        if entities[r["target"]]["name"] == "OnOrderSubmitted"
    ]
    assert calls_to_handler == []


# --- preserved uncertainty ----------------------------------------------------


def test_a_dynamic_call_is_recorded_as_unresolved_with_evidence(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Context section 3: never guess. Roslyn cannot name the target of a dynamic
    call, so the graph says so and keeps the call site."""
    dynamic_call = _member(entities, "Acme.App.OrderRunner", "DynamicCall")
    edges = _outgoing(relations, dynamic_call["id"], {"CALLS_UNRESOLVED"})
    assert len(edges) == 1

    edge = edges[0]
    assert edge["resolved"] is False
    assert edge["metadata"]["resolution_status"] == "Dynamic"
    assert edge["metadata"]["dispatch_kind"] == "Dynamic"
    assert edge["evidence"]["start_line"] > 0

    target = entities[edge["target"]]
    assert target["kind"] == "UnresolvedReference"
    assert "Submit" in target["metadata"]["expression"]


def test_reflection_is_recorded_alongside_the_resolved_api_call(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Two facts at one site: the call to Invoke is perfectly resolvable, and what
    it invokes is not. Recording only the first would hide the dependency."""
    reflection_call = _member(entities, "Acme.App.OrderRunner", "ReflectionCall")
    outgoing = _outgoing(relations, reflection_call["id"])

    resolved_api = [
        r
        for r in outgoing
        if r["kind"] == "CALLS_DIRECT" and entities[r["target"]]["name"] == "Invoke"
    ]
    assert len(resolved_api) == 1
    assert resolved_api[0]["metadata"]["resolution_status"] == "External"

    reflective = [
        r for r in outgoing if r["metadata"].get("resolution_status") == "ReflectionBased"
    ]
    assert len(reflective) == 2  # MethodBase.Invoke and Activator.CreateInstance
    for edge in reflective:
        assert edge["metadata"]["dispatch_kind"] == "Reflection"
        assert edge["resolved"] is False


def test_nameof_is_not_reported_as_an_unresolved_call(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """`nameof(x)` is invocation-shaped syntax with no call in it. Reporting these
    would fill the Unresolved bucket with noise and make the real entries
    worthless."""
    unresolved = [
        entities[r["target"]]["metadata"]["expression"]
        for r in relations
        if r["kind"] == "CALLS_UNRESOLVED"
    ]
    assert not any("nameof" in expression for expression in unresolved)


def test_a_cleanly_compiling_solution_has_no_spurious_unresolved_calls(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Every remaining unresolved call must be genuinely unknowable. If this fails,
    something resolvable is being dropped, and the Unresolved bucket stops meaning
    anything."""
    statuses = {
        r["metadata"]["resolution_status"]
        for r in relations
        if r["kind"] == "CALLS_UNRESOLVED"
    }
    assert statuses <= {"Dynamic", "ReflectionBased"}


def test_unresolved_references_are_identified_by_their_call_site(
    entities: dict[str, dict]
) -> None:
    """There is no symbol to name, so the location is the identity."""
    for entity in entities.values():
        if entity["kind"] != "UnresolvedReference":
            continue
        assert entity["canonical_name"].startswith("U:")
        assert entity["file_path"]
        assert entity["start_line"]


# --- counts -------------------------------------------------------------------


def test_call_counts_are_reported(scan: ExtractionResult) -> None:
    assert scan.manifest is not None
    assert scan.manifest.counts["calls"] > 30
    assert scan.manifest.counts["unresolved_calls"] >= 3


def test_a_degraded_variant_emits_no_calls(legacy_output: dict) -> None:
    """Same rule as declarations: without a core library a "call" says more about
    the build than about the code."""
    assert legacy_output["calls"] == []


@pytest.fixture(scope="module")
def legacy_output(tmp_path_factory: pytest.TempPathFactory) -> dict:
    from app.extractor_runner import run_extractor
    from tests.conftest import REPO_ROOT

    legacy = REPO_ROOT / "extractor" / "fixtures" / "LegacySolution"
    result = run_extractor(
        input_path=legacy / "LegacySolution.sln",
        output_dir=tmp_path_factory.mktemp("legacy-calls"),
        repo_id="legacy",
        scan_id="calls-degraded",
        repo_root=legacy,
    )
    relations = read_jsonl(result.output_dir / "relations.jsonl")
    return {"calls": [r for r in relations if r["kind"] in CALL_KINDS]}
