"""The C# and Python identity implementations must agree exactly.

Node ids are stored in Neo4j, so a divergence between the two halves would make
the writer create a second node for every symbol rather than matching the one
already there. Checked against literal digests so a change to either side is
visible in a diff.
"""

from __future__ import annotations

from app.contract import identity


def test_identity_version_is_recorded() -> None:
    assert identity.IDENTITY_VERSION == "v1"


def test_the_same_symbol_in_two_target_frameworks_is_two_nodes() -> None:
    """Because #if can give them different bodies and different calls."""
    modern = identity.symbol_id("M:A.B.C()", "src/A/A.csproj", "net8.0")
    legacy = identity.symbol_id("M:A.B.C()", "src/A/A.csproj", "net48")
    assert modern != legacy


def test_the_same_symbol_in_two_projects_is_two_nodes() -> None:
    a = identity.symbol_id("M:A.B.C()", "src/A/A.csproj", "net8.0")
    b = identity.symbol_id("M:A.B.C()", "src/B/B.csproj", "net8.0")
    assert a != b


def test_an_external_symbol_is_shared_across_projects() -> None:
    """One System.String node, not one per referencing project."""
    from_a = identity.external_symbol_id("T:System.String", "System.Private.CoreLib")
    from_b = identity.external_symbol_id("T:System.String", "System.Private.CoreLib")
    assert from_a == from_b


def test_a_namespace_is_shared_across_projects() -> None:
    """A namespace is a naming scope, not a compiled artifact."""
    assert identity.namespace_id("Acme.Orders") == identity.namespace_id("Acme.Orders")


def test_prefixes_identify_the_node_family() -> None:
    assert identity.repository_id("r").startswith("repo:")
    assert identity.project_id("p").startswith("proj:")
    assert identity.variant_id("p", "net8.0", "Debug").startswith("var:")
    assert identity.file_id("f").startswith("file:")
    assert identity.namespace_id("n").startswith("ns:")
    assert identity.symbol_id("c", "p", "net8.0").startswith("sym:")
    assert identity.external_symbol_id("c", "a").startswith("ext:")
    assert identity.unresolved_id("f", 1, 2, "x").startswith("unres:")


def test_digests_are_pinned() -> None:
    """Locks the hashing scheme. A change here means every stored id changed and
    a full re-scan is required, so it must be deliberate."""
    assert identity.symbol_id("M:A.B.C()", "src/A/A.csproj", "net8.0") == (
        "sym:1721a9b31c6d062044588c0d3381f0ea"
    )
    assert identity.external_symbol_id("T:System.String", "System.Private.CoreLib") == (
        "ext:b554b7737d651a483a75029b8feeea64"
    )
    assert identity.repository_id("sample") == "repo:551c141e0720ba1fecb21e942856b88e"
