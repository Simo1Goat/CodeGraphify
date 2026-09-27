"""Stage 4: the type hierarchy.

These edges are what turn a call graph into something answerable. A
CALLS_INTERFACE edge alone says a call went through IOrderService.Submit; only
IMPLEMENTS_MEMBER says which concrete methods could have run.
"""

from __future__ import annotations

from pathlib import Path

from app.extractor_runner import ExtractionResult, run_extractor
from tests.conftest import REPO_ROOT, read_jsonl, requires_dotnet, requires_extractor

pytestmark = [requires_extractor, requires_dotnet]

HIERARCHY_KINDS = {"INHERITS", "IMPLEMENTS", "OVERRIDES", "IMPLEMENTS_MEMBER"}


# --- helpers ------------------------------------------------------------------


def _type(entities: dict[str, dict], name: str) -> dict:
    matches = [
        e
        for e in entities.values()
        if e["name"] == name
        and e["kind"] in {"Class", "Record", "Struct", "Interface", "Enum", "Delegate"}
    ]
    assert len(matches) == 1, f"expected one type {name}, got {len(matches)}"
    return matches[0]


def _member(entities: dict[str, dict], declaring_type: str, name: str, params: int | None = None):
    matches = [
        e
        for e in entities.values()
        if e["name"] == name
        and e["metadata"].get("declaring_type") == declaring_type
        and (params is None or e["metadata"].get("parameter_count") == params)
    ]
    assert len(matches) == 1, f"expected one {declaring_type}.{name}, got {len(matches)}"
    return matches[0]


def _edges(relations: list[dict], kind: str, source_id: str) -> list[dict]:
    return [r for r in relations if r["kind"] == kind and r["source"] == source_id]


def _targets(entities: dict[str, dict], relations: list[dict], kind: str, source_id: str) -> set[str]:
    return {entities[r["target"]]["name"] for r in _edges(relations, kind, source_id)}


# --- integrity ----------------------------------------------------------------


def test_no_hierarchy_edge_dangles(entities: dict[str, dict], relations: list[dict]) -> None:
    """Synthesized members are the easy way to get one here: a record's Equals
    overrides and implements things without having any source of its own."""
    dangling = [
        r
        for r in relations
        if r["kind"] in HIERARCHY_KINDS
        and (r["source"] not in entities or r["target"] not in entities)
    ]
    assert dangling == []


def test_hierarchy_edges_carry_evidence_where_there_is_source_to_point_at(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    for relation in relations:
        if relation["kind"] not in {"INHERITS", "IMPLEMENTS"}:
            continue
        # A declared type always has source; only synthesized members may not.
        assert relation["evidence"] is not None
        assert relation["evidence"]["file_path"]
        assert relation["evidence"]["start_line"] >= 1


# --- inheritance --------------------------------------------------------------


def test_a_class_inherits_its_base_class(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    service = _type(entities, "OrderService")
    assert _targets(entities, relations, "INHERITS", service["id"]) == {"OrderServiceBase"}


def test_inheriting_an_external_base_is_recorded(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Unlike the implicit object base, deriving from a framework type is a real
    dependency and has to appear."""
    exception = _type(entities, "OrderException")
    edges = _edges(relations, "INHERITS", exception["id"])
    assert len(edges) == 1
    assert edges[0]["metadata"]["external_target"] is True
    target = entities[edges[0]["target"]]
    assert target["kind"] == "ExternalType"
    assert target["name"] == "InvalidOperationException"


def test_implicit_base_types_are_not_recorded(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Every class derives from object, every struct from ValueType, every enum from
    Enum. Recording that adds an edge per type to a handful of universal nodes and
    answers no question anyone asks."""
    universal = {"Object", "ValueType", "Enum", "Delegate", "MulticastDelegate", "Array"}
    bases = {
        entities[r["target"]]["name"] for r in relations if r["kind"] == "INHERITS"
    }
    assert bases & universal == set()


def test_an_interface_inherits_rather_than_implements_its_base_interfaces(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """An interface extends; it does not implement."""
    composite = _type(entities, "IAuditableOrderService")
    assert _targets(entities, relations, "INHERITS", composite["id"]) == {
        "IOrderService",
        "IAuditable",
    }
    assert _edges(relations, "IMPLEMENTS", composite["id"]) == []


# --- interfaces ---------------------------------------------------------------


def test_a_class_implements_its_declared_interfaces(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    service = _type(entities, "OrderService")
    assert "IOrderService" in _targets(entities, relations, "IMPLEMENTS", service["id"])


def test_implementing_an_external_interface_is_recorded(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    tracked = _type(entities, "TrackedOrderService")
    edges = [
        r
        for r in _edges(relations, "IMPLEMENTS", tracked["id"])
        if entities[r["target"]]["name"] == "IDisposable"
    ]
    assert len(edges) == 1
    assert edges[0]["metadata"]["external_target"] is True


def test_only_directly_declared_interfaces_are_recorded(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """TrackedOrderService inherits its IOrderService implementation from
    OrderService, so it declares only IAuditable and IDisposable.

    Storing the transitive closure would multiply edges and, worse, make "which type
    actually declares this relationship" unanswerable -- a graph query can compute
    the closure, but it cannot recover what was declared.
    """
    tracked = _type(entities, "TrackedOrderService")
    assert _targets(entities, relations, "IMPLEMENTS", tracked["id"]) == {
        "IAuditable",
        "IDisposable",
    }


def test_external_interfaces_are_keyed_by_definition_but_keep_the_constructed_form(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Order implements IEquatable<Order>. The node is IEquatable<T>, per the
    identity policy, with the instantiation kept on the edge."""
    order = _type(entities, "Order")
    edges = [
        r
        for r in _edges(relations, "IMPLEMENTS", order["id"])
        if entities[r["target"]]["name"] == "IEquatable"
    ]
    assert len(edges) == 1
    assert "Order" in edges[0]["metadata"]["interface"]


# --- overrides ----------------------------------------------------------------


def test_a_method_override_is_recorded(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    override = _member(entities, "Acme.Core.Orders.OrderService", "Validate")
    edges = _edges(relations, "OVERRIDES", override["id"])
    assert len(edges) == 1
    assert entities[edges[0]["target"]]["metadata"]["declaring_type"] == (
        "Acme.Core.Orders.OrderServiceBase"
    )


def test_a_property_override_is_recorded(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """OVERRIDES applies to properties too, not only methods."""
    override = _member(entities, "Acme.Core.Orders.OrderService", "Kind")
    assert override["kind"] == "Property"
    edges = _edges(relations, "OVERRIDES", override["id"])
    assert len(edges) == 1
    assert entities[edges[0]["target"]]["kind"] == "Property"


def test_implementing_an_abstract_member_is_an_override(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    describe = _member(entities, "Acme.Core.Orders.OrderService", "Describe")
    assert len(_edges(relations, "OVERRIDES", describe["id"])) == 1


def test_overrides_of_object_members_are_not_recorded(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Every record synthesizes ToString, Equals and GetHashCode over object's.
    Recording them is as uninformative as recording the object base itself, and
    leaving one in while skipping the other would be incoherent."""
    targets = {
        entities[r["target"]]["name"] for r in relations if r["kind"] == "OVERRIDES"
    }
    assert targets & {"ToString", "GetHashCode", "Equals"} == set()


def test_no_accessor_carries_an_override_edge(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """The property carries it; accessors are not nodes."""
    for relation in relations:
        if relation["kind"] != "OVERRIDES":
            continue
        assert not entities[relation["source"]]["name"].startswith(("get_", "set_"))


# --- interface member implementation ------------------------------------------


def test_an_implicit_implementation_is_paired_with_its_interface_member(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    cancel = _member(entities, "Acme.Core.Orders.OrderService", "Cancel")
    edges = _edges(relations, "IMPLEMENTS_MEMBER", cancel["id"])
    assert len(edges) == 1
    assert edges[0]["metadata"]["is_explicit"] is False
    assert entities[edges[0]["target"]]["metadata"]["declaring_type"] == (
        "Acme.Core.Orders.IOrderService"
    )


def test_an_explicit_implementation_is_found_despite_its_name(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """ExplicitOrderService's members are called
    'Acme.Core.Orders.IOrderService.Submit'. Matching on name would miss these
    entirely, which is why FindImplementationForInterfaceMember is used instead."""
    explicit = [
        e
        for e in entities.values()
        if e["metadata"].get("declaring_type") == "Acme.Core.Orders.ExplicitOrderService"
        and e["kind"] == "Method"
    ]
    assert len(explicit) == 3

    for member in explicit:
        edges = _edges(relations, "IMPLEMENTS_MEMBER", member["id"])
        assert len(edges) == 1, member["name"]
        assert edges[0]["metadata"]["is_explicit"] is True


def test_overloads_are_paired_with_the_matching_interface_overload(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """The one-argument Submit must implement the one-argument IOrderService.Submit.
    Pairing them by name would cross-wire the overloads."""
    for parameter_count in (1, 2):
        implementation = _member(
            entities, "Acme.Core.Orders.OrderService", "Submit", params=parameter_count
        )
        edges = _edges(relations, "IMPLEMENTS_MEMBER", implementation["id"])
        assert len(edges) == 1
        target = entities[edges[0]["target"]]
        assert target["metadata"]["declaring_type"] == "Acme.Core.Orders.IOrderService"
        assert target["metadata"]["parameter_count"] == parameter_count


def test_an_inherited_implementation_is_not_re_emitted_on_every_derived_type(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """TrackedOrderService satisfies IOrderService through OrderService.

    FindImplementationForInterfaceMember happily returns the base class's member for
    every derived type, so without a check that the implementation is declared *here*
    the same edge is re-emitted once per derived type -- growing quadratically down a
    deep hierarchy.
    """
    submits = [
        r
        for r in relations
        if r["kind"] == "IMPLEMENTS_MEMBER"
        and entities[r["target"]]["metadata"].get("declaring_type")
        == "Acme.Core.Orders.IOrderService"
        and entities[r["target"]]["name"] == "Submit"
        and entities[r["target"]]["metadata"]["parameter_count"] == 1
    ]
    declaring = {entities[r["source"]]["metadata"]["declaring_type"] for r in submits}
    assert "Acme.Core.Orders.TrackedOrderService" not in declaring
    assert declaring == {
        "Acme.Core.Orders.OrderService",
        "Acme.Core.Orders.MockOrderService",
        "Acme.Core.Orders.ExplicitOrderService",
    }


def test_implementing_an_external_interface_member_is_recorded(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    dispose = _member(entities, "Acme.Core.Orders.TrackedOrderService", "Dispose")
    edges = _edges(relations, "IMPLEMENTS_MEMBER", dispose["id"])
    assert len(edges) == 1
    assert edges[0]["metadata"]["external_target"] is True
    assert entities[edges[0]["target"]]["name"] == "Dispose"


# --- the payoff ---------------------------------------------------------------


def test_an_interface_call_can_be_traversed_to_what_might_actually_run(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """The question the whole feature exists to answer.

    CALLS_INTERFACE gives the compile-time target; following IMPLEMENTS_MEMBER
    backwards from it gives the concrete members that could run. Phase 2's
    MAY_DISPATCH_TO is a materialization of this traversal, not a new analysis.
    """
    run_all = _member(entities, "Acme.App.OrderRunner", "RunAll")

    interface_calls = [
        r
        for r in relations
        if r["kind"] == "CALLS_INTERFACE"
        and r["source"] == run_all["id"]
        and entities[r["target"]]["name"] == "Submit"
        and entities[r["target"]]["metadata"]["parameter_count"] == 1
    ]
    assert len(interface_calls) == 1

    implementations = {
        entities[r["source"]]["metadata"]["declaring_type"]
        for r in relations
        if r["kind"] == "IMPLEMENTS_MEMBER" and r["target"] == interface_calls[0]["target"]
    }
    assert implementations == {
        "Acme.Core.Orders.OrderService",
        "Acme.Core.Orders.MockOrderService",
        "Acme.Core.Orders.ExplicitOrderService",
    }


def test_the_two_overloads_reach_different_implementation_sets(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Semantic analysis earning its cost: a name-based graph would give both
    overloads the same six implementations."""
    run_all = _member(entities, "Acme.App.OrderRunner", "RunAll")
    by_arity: dict[int, set[str]] = {}

    for call in relations:
        if call["kind"] != "CALLS_INTERFACE" or call["source"] != run_all["id"]:
            continue
        target = entities[call["target"]]
        if target["name"] != "Submit":
            continue
        by_arity[target["metadata"]["parameter_count"]] = {
            r["source"]
            for r in relations
            if r["kind"] == "IMPLEMENTS_MEMBER" and r["target"] == call["target"]
        }

    assert set(by_arity) == {1, 2}
    assert by_arity[1] & by_arity[2] == set(), "overloads must not share implementations"


# --- scope and counts ---------------------------------------------------------


def test_hierarchy_counts_are_reported(scan: ExtractionResult) -> None:
    assert scan.manifest is not None
    assert scan.manifest.counts["hierarchy_edges"] > 10
    assert scan.manifest.counts["member_edges"] > 10


def test_a_degraded_variant_emits_no_hierarchy(tmp_path: Path) -> None:
    legacy = REPO_ROOT / "extractor" / "fixtures" / "LegacySolution"
    result = run_extractor(
        input_path=legacy / "LegacySolution.sln",
        output_dir=tmp_path / "out",
        repo_id="legacy",
        scan_id="tm-degraded",
        repo_root=legacy,
    )
    relations = read_jsonl(result.output_dir / "relations.jsonl")
    assert [r for r in relations if r["kind"] in HIERARCHY_KINDS] == []


def test_hierarchy_is_emitted_per_variant(scan_all_tfms: ExtractionResult) -> None:
    """A variant's hierarchy is its own: conditional compilation can change which
    interfaces a type implements."""
    entities = {e["id"]: e for e in read_jsonl(scan_all_tfms.output_dir / "entities.jsonl")}
    relations = read_jsonl(scan_all_tfms.output_dir / "relations.jsonl")

    frameworks = {
        r["metadata"]["target_framework"]
        for r in relations
        if r["kind"] == "INHERITS" and r["source"] in entities
    }
    assert frameworks == {"net8.0", "netstandard2.0"}
