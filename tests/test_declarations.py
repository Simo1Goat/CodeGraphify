"""Stage 2: the types and members the compilation declares.

The claims worth testing are the ones where a naive implementation quietly gets
it wrong: overloads collapsing together, accessors doubling the member count,
partial classes splitting into two nodes, and conditional code being attributed
to the wrong variant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.extractor_runner import ExtractionResult, run_extractor
from tests.conftest import (
    REPO_ROOT,
    read_jsonl,
    requires_dotnet,
    requires_extractor,
)

pytestmark = [requires_extractor, requires_dotnet]

DECLARATION_KINDS = {
    "Class",
    "Record",
    "Struct",
    "Interface",
    "Enum",
    "Delegate",
    "Method",
    "Constructor",
    "Property",
    "Field",
    "Event",
}


def _named(entities: dict[str, dict], name: str, kind: str | None = None) -> list[dict]:
    return [
        e
        for e in entities.values()
        if e["name"] == name and (kind is None or e["kind"] == kind)
    ]


def _one(entities: dict[str, dict], name: str, kind: str | None = None) -> dict:
    matches = _named(entities, name, kind)
    assert len(matches) == 1, f"expected exactly one {name}, got {len(matches)}"
    return matches[0]


# --- type kinds ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("OrderService", "Class"),
        ("Order", "Record"),
        ("Money", "Struct"),
        ("IOrderService", "Interface"),
        ("OrderState", "Enum"),
        ("OrderSubmitted", "Delegate"),
    ],
)
def test_type_kinds_are_mapped(entities: dict[str, dict], name: str, kind: str) -> None:
    assert _one(entities, name)["kind"] == kind


def test_a_record_is_recorded_as_a_record_not_a_class(entities: dict[str, dict]) -> None:
    order = _one(entities, "Order")
    assert order["kind"] == "Record"
    assert order["metadata"]["is_record"] is True
    assert order["metadata"]["is_value_type"] is False


# --- overloads and canonical names --------------------------------------------


def test_overloads_are_separate_nodes(entities: dict[str, dict]) -> None:
    """The whole point of semantic analysis: Submit(Order) and
    Submit(Order, bool) are different methods, and a name-based graph would
    merge them."""
    submits = [
        e
        for e in entities.values()
        if e["name"] == "Submit"
        and e["metadata"].get("declaring_type") == "Acme.Core.Orders.OrderService"
    ]
    assert len(submits) == 2
    assert {e["metadata"]["parameter_count"] for e in submits} == {1, 2}
    assert len({e["canonical_name"] for e in submits}) == 2


def test_canonical_names_are_documentation_comment_ids(entities: dict[str, dict]) -> None:
    """Roslyn's documentation id is the one name that is unique per symbol and
    stable across recompiles."""
    method = next(
        e
        for e in entities.values()
        if e["name"] == "Cancel"
        and e["metadata"].get("declaring_type") == "Acme.Core.Orders.OrderService"
    )
    assert method["canonical_name"] == "M:Acme.Core.Orders.OrderService.Cancel(System.Int32)"

    assert _one(entities, "IOrderService")["canonical_name"] == "T:Acme.Core.Orders.IOrderService"


def test_generic_types_and_members_keep_their_arity(entities: dict[str, dict]) -> None:
    repository = _one(entities, "Repository")
    assert repository["canonical_name"] == "T:Acme.Core.Data.Repository`1"
    assert repository["metadata"]["is_generic"] is True
    assert repository["metadata"]["type_parameter_count"] == 1

    add = next(
        e for e in entities.values()
        if e["name"] == "Add" and e["metadata"].get("declaring_type", "").startswith(
            "Acme.Core.Data.Repository"
        )
    )
    assert add["canonical_name"] == "M:Acme.Core.Data.Repository`1.Add(`0)"


def test_explicit_interface_implementations_are_declared(entities: dict[str, dict]) -> None:
    """A name match would miss these; only the symbol model finds them."""
    explicit = [
        e
        for e in entities.values()
        if e["metadata"].get("declaring_type") == "Acme.Core.Orders.ExplicitOrderService"
        and e["kind"] == "Method"
    ]
    assert len(explicit) == 3
    assert all("IOrderService" in e["name"] for e in explicit)


def test_extension_methods_are_flagged(entities: dict[str, dict]) -> None:
    add_all = _one(entities, "AddAll")
    assert add_all["metadata"]["is_extension_method"] is True


# --- what must NOT become a node ----------------------------------------------


def test_property_accessors_are_not_emitted_as_methods(entities: dict[str, dict]) -> None:
    """Accessors would double the member count and split calls across two nodes.

    A call to a property getter is attributed to the Property instead.
    """
    accessors = [
        e
        for e in entities.values()
        if e["kind"] == "Method" and (e["name"].startswith(("get_", "set_")))
    ]
    assert accessors == []


def test_event_accessors_are_not_emitted_as_methods(entities: dict[str, dict]) -> None:
    accessors = [
        e
        for e in entities.values()
        if e["kind"] == "Method" and e["name"].startswith(("add_", "remove_"))
    ]
    assert accessors == []


def test_property_backing_fields_are_not_emitted(entities: dict[str, dict]) -> None:
    """A backing field has no declaration of its own and no analytical value."""
    backing = [
        e for e in entities.values() if e["kind"] == "Field" and "BackingField" in e["name"]
    ]
    assert backing == []


def test_parameters_are_not_nodes_but_are_still_described(
    entities: dict[str, dict]
) -> None:
    """Phase 1 keeps parameters as metadata: they would multiply node count on a
    heavy repo for little analytical value, but a signature still has to be
    readable and overloads still have to be tellable apart."""
    assert [e for e in entities.values() if e["kind"] == "Parameter"] == []

    submit = next(
        e
        for e in entities.values()
        if e["name"] == "Submit"
        and e["metadata"].get("declaring_type") == "Acme.Core.Orders.OrderService"
        and e["metadata"]["parameter_count"] == 2
    )
    assert submit["metadata"]["parameter_types"] == [
        "Acme.Core.Orders.Order",
        "bool",
    ]
    assert "Submit" in submit["metadata"]["signature"]


# --- what must become a node despite being synthesized ------------------------


def test_record_positional_properties_are_emitted(entities: dict[str, dict]) -> None:
    """They are implicitly declared, so a blanket skip of synthesized members
    would drop the entire surface of every positional record."""
    order_properties = {
        e["name"]
        for e in entities.values()
        if e["kind"] == "Property"
        and e["metadata"].get("declaring_type") == "Acme.Core.Orders.Order"
    }
    assert {"Id", "Total", "State"} <= order_properties


def test_enum_members_are_emitted_as_fields(entities: dict[str, dict]) -> None:
    members = {
        e["name"]
        for e in entities.values()
        if e["kind"] == "Field"
        and e["metadata"].get("declaring_type") == "Acme.Core.Orders.OrderState"
    }
    assert members == {"Draft", "Submitted", "Cancelled"}


def test_events_are_emitted(entities: dict[str, dict]) -> None:
    submitted = _one(entities, "Submitted", "Event")
    assert submitted["metadata"]["event_type"] == "Acme.Core.Orders.OrderSubmitted"


# --- member modifiers ---------------------------------------------------------


def test_virtual_and_override_are_recorded(entities: dict[str, dict]) -> None:
    """Stage 4 builds OVERRIDES from these, and stage 3 chooses between
    CALLS_DIRECT and CALLS_VIRTUAL by them."""
    base = next(
        e for e in entities.values()
        if e["name"] == "Validate"
        and e["metadata"]["declaring_type"] == "Acme.Core.Orders.OrderServiceBase"
    )
    assert base["metadata"]["is_virtual"] is True
    assert base["metadata"]["is_override"] is False

    derived = next(
        e for e in entities.values()
        if e["name"] == "Validate"
        and e["metadata"]["declaring_type"] == "Acme.Core.Orders.OrderService"
    )
    assert derived["metadata"]["is_override"] is True


def test_abstract_members_are_recorded(entities: dict[str, dict]) -> None:
    describe = next(
        e for e in entities.values()
        if e["name"] == "Describe"
        and e["metadata"]["declaring_type"] == "Acme.Core.Orders.OrderServiceBase"
    )
    assert describe["metadata"]["is_abstract"] is True


def test_accessibility_is_recorded(entities: dict[str, dict]) -> None:
    field = next(
        e for e in entities.values()
        if e["name"] == "_orders" and e["kind"] == "Field"
    )
    assert field["metadata"]["accessibility"] == "Private"
    assert _one(entities, "IOrderService")["metadata"]["accessibility"] == "Public"


# --- containment --------------------------------------------------------------


def test_types_are_contained_by_their_namespace(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    service = _one(entities, "OrderService")
    containers = [
        entities[r["source"]]
        for r in relations
        if r["kind"] == "CONTAINS" and r["target"] == service["id"]
    ]
    assert len(containers) == 1
    assert containers[0]["kind"] == "Namespace"
    assert containers[0]["metadata"]["full_name"] == "Acme.Core.Orders"


def test_members_are_declared_by_their_type(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    service = _one(entities, "OrderService")
    declared = {
        entities[r["target"]]["name"]
        for r in relations
        if r["kind"] == "DECLARES" and r["source"] == service["id"]
    }
    assert {"Submit", "Cancel", "Validate", "Describe", "_orders", "Submitted"} <= declared


def test_nested_types_are_contained_by_the_enclosing_type(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Containment is lexical, so a nested type hangs off its enclosing type, not
    off the namespace."""
    failure = _one(entities, "Failure")
    assert failure["canonical_name"] == "T:Acme.Core.Orders.OrderValidator.Failure"
    containers = [
        entities[r["source"]]
        for r in relations
        if r["kind"] == "CONTAINS" and r["target"] == failure["id"]
    ]
    assert [c["name"] for c in containers] == ["OrderValidator"]

    # And nesting composes: an enum inside a nested class.
    severity = _one(entities, "Severity")
    severity_containers = [
        entities[r["source"]]
        for r in relations
        if r["kind"] == "CONTAINS" and r["target"] == severity["id"]
    ]
    assert [c["name"] for c in severity_containers] == ["Failure"]


def test_nested_namespaces_form_a_chain(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    namespaces = {
        e["metadata"]["full_name"]: e for e in entities.values() if e["kind"] == "Namespace"
    }
    assert {"Acme", "Acme.Core", "Acme.Core.Orders"} <= set(namespaces)

    parents = [
        entities[r["source"]]
        for r in relations
        if r["kind"] == "CONTAINS" and r["target"] == namespaces["Acme.Core.Orders"]["id"]
    ]
    assert [p["metadata"]["full_name"] for p in parents] == ["Acme.Core"]


# --- partial types ------------------------------------------------------------


def test_a_partial_class_is_one_node_declared_in_several_files(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Walking syntax trees would find this class twice. Walking symbols finds it
    once, with two declaring references, which is what the graph should say."""
    validator = _one(entities, "OrderValidator")
    assert validator["metadata"]["is_partial"] is True
    # Compared by basename: the path prefix depends on the repo root the scan
    # was given, which is not what this test is about.
    assert sorted(
        path.rsplit("/", 1)[-1] for path in validator["metadata"]["declaration_files"]
    ) == ["Validation.Identity.cs", "Validation.Rules.cs"]

    declaring_files = sorted(
        entities[r["source"]]["name"]
        for r in relations
        if r["kind"] == "DECLARES" and r["target"] == validator["id"]
    )
    assert declaring_files == ["Validation.Identity.cs", "Validation.Rules.cs"]


def test_members_of_both_halves_of_a_partial_class_are_declared(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    validator = _one(entities, "OrderValidator")
    declared = {
        entities[r["target"]]["name"]
        for r in relations
        if r["kind"] == "DECLARES" and r["source"] == validator["id"]
    }
    assert {"HasPositiveTotal", "HasIdentity", "Validate"} <= declared


def test_a_non_partial_type_is_not_flagged_partial(entities: dict[str, dict]) -> None:
    assert _one(entities, "OrderService")["metadata"]["is_partial"] is False


# --- per-variant declarations -------------------------------------------------


def test_conditional_members_are_attributed_to_the_right_variant(
    scan_all_tfms: ExtractionResult,
) -> None:
    """The headline claim of per-variant analysis: one source file yields
    different declarations depending on the target framework."""
    entities = {e["id"]: e for e in read_jsonl(scan_all_tfms.output_dir / "entities.jsonl")}

    def members_of(target_framework: str) -> set[str]:
        return {
            e["name"]
            for e in entities.values()
            if e["metadata"].get("declaring_type") == "Acme.Core.PlatformInfo"
            and e["metadata"].get("target_framework") == target_framework
        }

    assert members_of("net8.0") == {"Name", "ModernOnly"}
    assert members_of("netstandard2.0") == {"Name", "LegacyOnly"}


def test_a_type_only_compiled_into_one_variant_appears_once(
    scan_all_tfms: ExtractionResult,
) -> None:
    entities = read_jsonl(scan_all_tfms.output_dir / "entities.jsonl")
    shims = [e for e in entities if e["name"] == "LegacyShim"]
    assert len(shims) == 1
    assert shims[0]["metadata"]["target_framework"] == "netstandard2.0"


def test_the_same_type_in_two_variants_is_two_nodes(
    scan_all_tfms: ExtractionResult,
) -> None:
    """Because #if can give them different members, as proved above."""
    entities = read_jsonl(scan_all_tfms.output_dir / "entities.jsonl")
    services = [e for e in entities if e["name"] == "OrderService" and e["kind"] == "Class"]
    assert len(services) == 2
    assert {e["metadata"]["target_framework"] for e in services} == {
        "net8.0",
        "netstandard2.0",
    }
    assert len({e["id"] for e in services}) == 2


def test_namespaces_are_shared_across_variants(scan_all_tfms: ExtractionResult) -> None:
    """A namespace is a naming scope, not a compiled artifact, so a unified
    namespace tree is more useful than one copy per variant."""
    entities = read_jsonl(scan_all_tfms.output_dir / "entities.jsonl")
    orders = [
        e
        for e in entities
        if e["kind"] == "Namespace" and e["metadata"]["full_name"] == "Acme.Core.Orders"
    ]
    assert len(orders) == 1


# --- degradation --------------------------------------------------------------


def test_a_degraded_variant_emits_no_declarations(tmp_path: Path) -> None:
    """Without a core library every symbol resolves to an error type, so emitting
    declarations would put fabricated types in the graph."""
    legacy = REPO_ROOT / "extractor" / "fixtures" / "LegacySolution"
    result = run_extractor(
        input_path=legacy / "LegacySolution.sln",
        output_dir=tmp_path / "out",
        repo_id="legacy",
        scan_id="decl-degraded",
        repo_root=legacy,
    )
    entities = read_jsonl(result.output_dir / "entities.jsonl")
    assert [e for e in entities if e["kind"] in DECLARATION_KINDS] == []


def test_declaration_count_is_reported(scan: ExtractionResult) -> None:
    assert scan.manifest is not None
    assert scan.manifest.counts["declarations"] > 50
