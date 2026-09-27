"""The graph contract between any analyzer and the Neo4j writer.

Language-neutral on purpose: the Roslyn extractor and the existing tree-sitter
fallback both emit this shape, so the writer never learns which one produced a
payload beyond the ``resolution_engine`` recorded on each relation.

These models are the source of truth. ``scripts/gen_schema.py`` regenerates
``contract/schema/*.json`` from them; the extractor's output is validated
against those models in ``tests/``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class AnalysisMode(StrEnum):
    """How much the analyzer was actually able to prove (context section 3.2)."""

    SEMANTIC = "semantic"
    PARTIAL_SEMANTIC = "partial-semantic"
    SYNTAX_ONLY = "syntax-only"
    FAILED = "failed"


class EntityKind(StrEnum):
    # Structural
    REPOSITORY = "Repository"
    SOLUTION = "Solution"
    PROJECT = "Project"
    COMPILATION_VARIANT = "CompilationVariant"
    FILE = "File"
    GENERATED_FILE = "GeneratedFile"
    # Types
    NAMESPACE = "Namespace"
    CLASS = "Class"
    RECORD = "Record"
    STRUCT = "Struct"
    INTERFACE = "Interface"
    ENUM = "Enum"
    DELEGATE = "Delegate"
    # Members
    METHOD = "Method"
    CONSTRUCTOR = "Constructor"
    PROPERTY = "Property"
    FIELD = "Field"
    EVENT = "Event"
    LOCAL_FUNCTION = "LocalFunction"
    # Phase 2
    ROUTE = "Route"
    # Outside the analyzed projects.
    # ExternalAssembly is a referenced assembly as a whole; ExternalType is a type
    # inside one. Kept apart so "which libraries do we depend on" and "which of
    # their types do we actually touch" are different questions.
    EXTERNAL_ASSEMBLY = "ExternalAssembly"
    EXTERNAL_TYPE = "ExternalType"
    EXTERNAL_METHOD = "ExternalMethod"
    UNRESOLVED_REFERENCE = "UnresolvedReference"


class RelationKind(StrEnum):
    # Structural
    CONTAINS = "CONTAINS"
    DECLARES = "DECLARES"
    COMPILES = "COMPILES"
    COMPILES_AS = "COMPILES_AS"
    GENERATES = "GENERATES"
    REFERENCES_PROJECT = "REFERENCES_PROJECT"
    REFERENCES_ASSEMBLY = "REFERENCES_ASSEMBLY"
    # Type model
    INHERITS = "INHERITS"
    IMPLEMENTS = "IMPLEMENTS"
    OVERRIDES = "OVERRIDES"
    IMPLEMENTS_MEMBER = "IMPLEMENTS_MEMBER"
    # Calls
    CALLS_DIRECT = "CALLS_DIRECT"
    CALLS_VIRTUAL = "CALLS_VIRTUAL"
    CALLS_INTERFACE = "CALLS_INTERFACE"
    # A call site whose target cannot be named: dynamic, reflective, ambiguous or
    # broken. Points at an UnresolvedReference carrying the evidence. Context
    # section 3.3 requires preserving these rather than fabricating an edge or
    # dropping the call, and none of the CALLS_* kinds above can be used without
    # asserting a dispatch that was never proved.
    CALLS_UNRESOLVED = "CALLS_UNRESOLVED"
    MAY_DISPATCH_TO = "MAY_DISPATCH_TO"
    # Data / other
    CREATES = "CREATES"
    READS = "READS"
    WRITES = "WRITES"
    ROUTE_HANDLED_BY = "ROUTE_HANDLED_BY"
    RESOLVES_TO = "RESOLVES_TO"
    # Derived in Neo4j as a roll-up, never emitted per call site.
    DEPENDS_ON = "DEPENDS_ON"


class ResolutionStatus(StrEnum):
    """Per-relation certainty. Uncertainty is recorded, never guessed away."""

    RESOLVED = "Resolved"
    AMBIGUOUS = "Ambiguous"
    EXTERNAL = "External"
    VIRTUAL_DISPATCH = "VirtualDispatch"
    INTERFACE_DISPATCH = "InterfaceDispatch"
    DYNAMIC = "Dynamic"
    REFLECTION_BASED = "ReflectionBased"
    BUILD_UNAVAILABLE = "BuildUnavailable"
    PARSE_ERROR = "ParseError"
    UNRESOLVED = "Unresolved"


class DispatchKind(StrEnum):
    """How the call reaches its target, recorded separately from how confidently
    the target was identified.

    Both are needed: a virtual call into a referenced assembly is
    ``resolution_status=External`` and ``dispatch_kind=Virtual``, and collapsing
    them into one field loses whichever half is written second.
    """

    DIRECT = "Direct"
    VIRTUAL = "Virtual"
    INTERFACE = "Interface"
    DYNAMIC = "Dynamic"
    REFLECTION = "Reflection"
    NONE = "None"


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompilationContext(Base):
    """Context section 8. Part of both identity and evidence.

    The same source file can yield different active syntax under a different
    target framework or configuration, so entities and relations must be able to
    say which variant they came from.
    """

    variant_id: str = Field(description="Stable id of this CompilationVariant.")
    project_key: str = Field(description="Repo-relative .csproj path.")
    project_path: str
    assembly_name: str | None = None
    target_framework: str | None = None
    configuration: str = "Debug"
    language_version: str | None = None
    nullable_context: str | None = None
    preprocessor_symbols: tuple[str, ...] = ()
    is_primary_variant: bool = Field(
        default=True,
        description="True for the one variant per project that was analyzed by "
        "default. Multi-targeting projects have further variants only when the "
        "caller asked for them.",
    )


class SourceSpan(Base):
    """Where in the source a fact was observed. Lines and columns are 1-based."""

    file_path: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int


class Entity(Base):
    id: str = Field(description="Stable node id; see app/contract/identity.py.")
    kind: EntityKind
    name: str
    canonical_name: str = Field(
        description="Roslyn documentation comment id for symbols "
        "(M:Acme.Orders.OrderService.Submit(Acme.Orders.Order)), or a "
        "repo-relative path for files and projects."
    )
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class Relation(Base):
    source: str = Field(description="Source entity id.")
    target: str = Field(description="Target entity id.")
    kind: RelationKind
    resolved: bool
    evidence: SourceSpan | None = Field(
        default=None,
        description="Required for every semantic relation. Structural relations "
        "(CONTAINS, COMPILES) have no single call site and may omit it.",
    )
    metadata: dict[str, object] = Field(default_factory=dict)


class Diagnostic(Base):
    severity: DiagnosticSeverity
    code: str
    message: str
    project_key: str | None = None
    file_path: str | None = None
    start_line: int | None = None


class OutputFile(Base):
    """One JSONL shard written next to the manifest."""

    path: str = Field(description="Path relative to the output directory.")
    kind: str = Field(description="entities | relations | diagnostics | compilations")
    count: int


class Manifest(Base):
    """The small JSON summary the extractor prints to stdout and writes to
    ``manifest.json``. Bulk data lives in the JSONL files it points at.
    """

    schema_version: int = SCHEMA_VERSION
    repo_id: str
    scan_id: str
    analysis_mode: AnalysisMode
    resolution_engine: str = Field(description="roslyn | tree-sitter")
    started_at: str
    finished_at: str
    repo_root: str
    # Scope of what was actually analyzed. The stale sweep may never delete
    # outside this set (context section 4, Stage 4 realism note).
    analyzed_project_keys: tuple[str, ...] = ()
    failed_project_keys: tuple[str, ...] = ()
    counts: dict[str, int] = Field(default_factory=dict)
    output_files: tuple[OutputFile, ...] = ()
    errors: tuple[str, ...] = ()
