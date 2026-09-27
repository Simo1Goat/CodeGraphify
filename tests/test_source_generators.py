"""Source generators, and why the extractor has to run them itself.

MSBuildWorkspace loads a project's generator references but never runs them:
``GetCompilationAsync`` returns only hand-written files and
``GetSourceGeneratedDocumentsAsync`` returns nothing. Measured on a real ASP.NET
reference application: eleven generators loaded, zero documents produced.

That is not a detail. Every type declared in a ``.razor`` or ``.cshtml`` file is
generator output, so without running them those types do not exist, every reference
to one is an error, and the project is written off as having missing references --
which withholds the stale sweep for the entire scan. Running them on eShopOnWeb took
the call count from 2800 to 6963 and the scan from partial-semantic to semantic.

The fixture stands in for that with ``[GeneratedRegex]``, which needs only the SDK.
"""

from __future__ import annotations

from app.extractor_runner import ExtractionResult
from tests.conftest import read_jsonl, requires_dotnet, requires_extractor

pytestmark = [requires_extractor, requires_dotnet]


def _generated_files(entities: dict[str, dict]) -> list[dict]:
    return [e for e in entities.values() if e["metadata"].get("source_generated")]


def test_generator_output_reaches_the_graph(entities: dict[str, dict]) -> None:
    generated = _generated_files(entities)
    assert generated, "no source-generated files; generators are not being run"
    assert any("RegexGenerator" in e["canonical_name"] for e in generated)


def test_generated_files_are_marked_and_kept_apart_from_real_ones(
    entities: dict[str, dict],
) -> None:
    """A generated tree has no file on disk. Resolving its synthetic path against the
    working directory would invent a location, so it is marked instead."""
    for entity in _generated_files(entities):
        assert entity["kind"] == "GeneratedFile"
        assert entity["canonical_name"].startswith("<generated>/")
        assert entity["metadata"]["generated"] is True


def test_generated_files_are_linked_to_the_variant_that_produced_them(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Otherwise their declarations hang off a node nothing reaches."""
    for generated in _generated_files(entities):
        producers = [
            r
            for r in relations
            if r["kind"] == "GENERATES" and r["target"] == generated["id"]
        ]
        assert producers, generated["name"]
        assert all(entities[r["source"]]["kind"] == "CompilationVariant" for r in producers)


def test_a_partial_type_spans_its_written_and_generated_halves(
    entities: dict[str, dict]
) -> None:
    """The point of running generators: the two halves are one type."""
    text_rules = [e for e in entities.values() if e["name"] == "TextRules"]
    assert len(text_rules) == 1

    files = text_rules[0]["metadata"]["declaration_files"]
    assert any(f.endswith("TextRules.cs") for f in files), files
    assert any(f.startswith("<generated>/") for f in files), files


def test_the_generated_half_is_not_duplicated(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """The workspace's compilation can already carry generator output from a previous
    build, as a tree with no document behind it. Running the generators over that
    produced a second copy of every generated type -- 'a partial method may not have
    multiple implementing declarations', and every call to one became ambiguous.
    """
    files = next(
        e["metadata"]["declaration_files"]
        for e in entities.values()
        if e["name"] == "TextRules"
    )
    generated = [f for f in files if "RegexGenerator" in f]
    assert len(generated) == 1, f"generated half appears {len(generated)} times: {files}"


def test_running_generators_introduces_no_compilation_errors(
    scan: ExtractionResult,
) -> None:
    """Double generation showed up as CS0757 and CS0121 rather than as a crash, so a
    clean fixture having no error diagnostics is what guards against it."""
    diagnostics = read_jsonl(scan.output_dir / "diagnostics.jsonl")
    errors = [d for d in diagnostics if d["severity"] == "error"]
    assert errors == [], errors


def test_generated_declarations_do_not_dangle(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """Stage 2 emits DECLARES from the file a type is declared in. For generator
    output that file is synthetic, and until it was inventoried those edges dangled --
    63 of them on the real solution."""
    generated_ids = {e["id"] for e in _generated_files(entities)}
    declares = [
        r for r in relations if r["kind"] == "DECLARES" and r["source"] in generated_ids
    ]
    assert declares, "expected declarations from generated files"
    assert all(r["target"] in entities for r in declares)


def test_a_call_into_generated_code_resolves(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """IsOrderCode calls the partial method the generator implements. Without the
    generated half this call would be unresolved."""
    is_order_code = next(
        e
        for e in entities.values()
        if e["name"] == "IsOrderCode" and e["kind"] == "Method"
    )
    targets = {
        entities[r["target"]]["name"]
        for r in relations
        if r["source"] == is_order_code["id"] and r["kind"].startswith("CALLS")
    }
    assert "OrderCode" in targets
    unresolved = [
        r
        for r in relations
        if r["source"] == is_order_code["id"] and r["kind"] == "CALLS_UNRESOLVED"
    ]
    assert unresolved == []
