"""Stage 1 behaviour: what the walk is supposed to discover.

These are the claims that make the compilation inventory worth having, rather
than a plain file listing.
"""

from __future__ import annotations

from pathlib import Path

from app.contract import identity
from app.extractor_runner import ExtractionResult, run_extractor
from tests.conftest import FIXTURE_SOLUTION, read_jsonl, requires_dotnet, requires_extractor

pytestmark = [requires_extractor, requires_dotnet]


def _by_kind(entities: dict[str, dict], kind: str) -> list[dict]:
    return [e for e in entities.values() if e["kind"] == kind]


def test_one_project_node_per_csproj_not_per_variant(entities: dict[str, dict]) -> None:
    projects = {e["canonical_name"].split("/")[-1] for e in _by_kind(entities, "Project")}
    assert projects == {"Core.csproj", "App.csproj"}


def test_multi_targeting_project_yields_one_primary_variant_by_default(
    compilations: list[dict],
) -> None:
    """Core targets net8.0 and netstandard2.0; only the preferred one is analyzed."""
    core = [c for c in compilations if c["project_key"].endswith("Core.csproj")]
    assert len(core) == 1
    assert core[0]["target_framework"] == "net8.0"
    assert core[0]["is_primary_variant"] is True


def test_all_target_frameworks_yields_every_variant(scan_all_tfms: ExtractionResult) -> None:
    compilations = read_jsonl(scan_all_tfms.output_dir / "compilations.jsonl")
    core = {c["target_framework"] for c in compilations if c["project_key"].endswith("Core.csproj")}
    assert core == {"net8.0", "netstandard2.0"}

    primaries = {
        c["target_framework"]
        for c in compilations
        if c["project_key"].endswith("Core.csproj") and c["is_primary_variant"]
    }
    assert primaries == {"net8.0"}, "exactly one variant per project may be primary"


def test_compilation_context_captures_every_identity_field(compilations: list[dict]) -> None:
    """Context section 8. A missing field here makes variants indistinguishable."""
    core = next(c for c in compilations if c["project_key"].endswith("Core.csproj"))
    assert core["assembly_name"] == "Acme.Core"
    assert core["target_framework"] == "net8.0"
    assert core["configuration"] == "Debug"
    assert core["language_version"].startswith("CSharp")
    assert core["nullable_context"] == "Enable"
    assert "NET8_0" in core["preprocessor_symbols"]
    assert "DEBUG" in core["preprocessor_symbols"]


def test_conditional_compile_items_differ_between_variants(
    scan_all_tfms: ExtractionResult,
) -> None:
    """The reason variants exist: the same project compiles different files.

    LegacyShim.cs is removed from the modern variant by a conditional Compile
    item, so a per-project view of the repo would get this wrong.
    """
    entities = {e["id"]: e for e in read_jsonl(scan_all_tfms.output_dir / "entities.jsonl")}
    relations = read_jsonl(scan_all_tfms.output_dir / "relations.jsonl")
    compilations = read_jsonl(scan_all_tfms.output_dir / "compilations.jsonl")

    variant_tfm = {
        c["variant_id"]: c["target_framework"]
        for c in compilations
        if c["project_key"].endswith("Core.csproj")
    }

    compiled: dict[str, set[str]] = {tfm: set() for tfm in variant_tfm.values()}
    for relation in relations:
        if relation["kind"] == "COMPILES" and relation["source"] in variant_tfm:
            name = entities[relation["target"]]["name"]
            compiled[variant_tfm[relation["source"]]].add(name)

    assert "LegacyShim.cs" in compiled["netstandard2.0"]
    assert "LegacyShim.cs" not in compiled["net8.0"]
    # A file with #if in it belongs to both; only its active syntax differs.
    assert "Conditional.cs" in compiled["net8.0"]
    assert "Conditional.cs" in compiled["netstandard2.0"]


def test_generated_documents_are_distinguished_from_repository_files(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """MSBuild-generated documents are compiled but are not in the repo."""
    generated = _by_kind(entities, "GeneratedFile")
    assert generated, "no generated documents found"
    names = {e["name"] for e in generated}
    assert any(name.endswith(".AssemblyInfo.cs") for name in names)
    assert all(e["metadata"]["generated"] is True for e in generated)

    generated_ids = {e["id"] for e in generated}
    edge_kinds = {
        r["kind"] for r in relations if r["target"] in generated_ids and r["kind"] in {
            "COMPILES", "GENERATES"
        }
    }
    assert edge_kinds == {"GENERATES"}, "generated documents must use GENERATES, not COMPILES"


def test_project_reference_is_recorded(entities: dict[str, dict], relations: list[dict]) -> None:
    references = [r for r in relations if r["kind"] == "REFERENCES_PROJECT"]
    assert len(references) == 1
    source = entities[references[0]["source"]]["canonical_name"]
    target = entities[references[0]["target"]]["canonical_name"]
    assert source.endswith("App.csproj")
    assert target.endswith("Core.csproj")


def test_assembly_references_are_shared_nodes_not_per_project_copies(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Both projects reference System.Runtime; that must be one node.

    This is what makes "who depends on this assembly" answerable. Note the
    reference set is the targeting pack, so the names are the reference
    assemblies (System.Runtime) rather than the implementation ones
    (System.Private.CoreLib).
    """
    runtime = [
        e
        for e in entities.values()
        if e["kind"] == "ExternalType" and e["name"] == "System.Runtime"
    ]
    assert len(runtime) == 1, "framework assembly duplicated per project"

    referrers = {r["source"] for r in relations if r["target"] == runtime[0]["id"]}
    assert len(referrers) == 2, "expected both projects' variants to reference it"


def test_repository_inventory_uses_git_and_includes_non_code_files(
    scan: ExtractionResult, entities: dict[str, dict]
) -> None:
    """The walk reports project, build and documentation files, not only C#."""
    repository = next(e for e in entities.values() if e["kind"] == "Repository")
    assert repository["metadata"]["inventory_source"] == "git"

    categories = {
        e["metadata"].get("category")
        for e in entities.values()
        if e["kind"] == "File"
    }
    assert {"code", "project", "solution", "build"} <= categories


def test_filesystem_fallback_when_input_is_not_a_git_repo(tmp_path: Path) -> None:
    """A non-git directory must still produce an inventory."""
    import shutil

    source = FIXTURE_SOLUTION.parent
    copy = tmp_path / "SampleSolution"
    shutil.copytree(source, copy, ignore=shutil.ignore_patterns("bin", "obj"))

    result = run_extractor(
        input_path=copy / "SampleSolution.sln",
        output_dir=tmp_path / "out",
        repo_id="copied",
        scan_id="fs-fallback",
        repo_root=copy,
    )

    assert result.manifest is not None, result.stderr
    entities = {e["id"]: e for e in read_jsonl(result.output_dir / "entities.jsonl")}
    repository = next(e for e in entities.values() if e["kind"] == "Repository")
    assert repository["metadata"]["inventory_source"] == "filesystem"
    assert repository["metadata"]["file_count"] > 0

    # bin/ and obj/ must never enter the graph as repository files.
    paths = {e["canonical_name"] for e in entities.values() if e["kind"] == "File"}
    assert not any("/obj/" in p or "/bin/" in p for p in paths)


def test_paths_are_repo_relative_and_forward_slashed(entities: dict[str, dict]) -> None:
    """Ids must not change with the checkout location or the host OS."""
    for entity in entities.values():
        path = entity.get("file_path")
        if path is None:
            continue
        assert not path.startswith("/"), f"absolute path leaked: {path}"
        assert "\\" not in path, f"backslash leaked: {path}"


def test_ids_match_the_python_identity_policy(entities: dict[str, dict]) -> None:
    """The C# and Python halves of the identity policy must agree."""
    for entity in entities.values():
        match entity["kind"]:
            case "Repository":
                expected = identity.repository_id(entity["canonical_name"])
            case "Project":
                expected = identity.project_id(entity["canonical_name"])
            case "Solution":
                expected = identity.solution_id(entity["canonical_name"])
            case "File" | "GeneratedFile":
                expected = identity.file_id(entity["canonical_name"])
            case _:
                continue
        assert entity["id"] == expected, f"id policy drift on {entity['kind']}"


def test_variant_ids_match_the_python_identity_policy(compilations: list[dict]) -> None:
    for context in compilations:
        expected = identity.variant_id(
            context["project_key"], context["target_framework"], context["configuration"]
        )
        assert context["variant_id"] == expected


def test_scan_is_reproducible(scan: ExtractionResult, tmp_path: Path) -> None:
    """Two scans of unchanged source must produce identical ids.

    Without this, every scan looks like a complete rewrite of the graph and the
    stale sweep deletes and recreates everything.
    """
    again = run_extractor(
        input_path=FIXTURE_SOLUTION,
        output_dir=tmp_path / "again",
        repo_id="sample",
        scan_id="a-different-scan-id",
    )
    assert again.manifest is not None, again.stderr

    first = {e["id"] for e in read_jsonl(scan.output_dir / "entities.jsonl")}
    second = {e["id"] for e in read_jsonl(again.output_dir / "entities.jsonl")}
    assert first == second, "entity ids are not stable across scans"
