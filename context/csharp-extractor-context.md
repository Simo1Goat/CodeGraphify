# C# Dependency-Graph Extractor — Project Context

> Context file for the Roslyn-based C# analysis feature added to the existing
> FastAPI backend. Read this before working on the extractor, the subprocess
> bridge, or the Neo4j writer.

---

## 1. Purpose & Scope

Add a **C# code-analysis feature** to an existing FastAPI (Python) backend. The
feature parses a heavy .NET codebase into a **dependency / call graph**
(classes, methods, files, and how they reference each other) and ingests it into
**Neo4j**.

The graph must be accurate enough for real dependency analysis: correct call
resolution across overloads, inheritance, interfaces, and generics. That
accuracy requires **semantic** analysis (Roslyn `SemanticModel` over a compiled
`Compilation`), not syntax-only parsing.

**In scope:** C# extraction via Roslyn, a language-neutral graph contract, and
ingestion into Neo4j with incremental (per-scan) reconciliation.

**Out of scope (this feature):** re-implementing the existing tree-sitter parser
(see §3), non-C# languages, and anything requiring runtime execution of the
target code.

---

## 2. Architecture & Integration

### 2.1 Component layout

```
┌────────────────────┐   subprocess    ┌───────────────────────────┐
│ FastAPI backend    │  (args in,      │ Roslyn extractor (C#)     │
│ (Python)           │   files+JSON    │  MSBuildWorkspace          │
│                    │   summary out,  │   → Compilation            │
│  - orchestrates    │   exit code)    │   → SemanticModel per file │
│  - Neo4j writer    │ ───────────────▶│   → walk/parse/resolve     │
│  - tree-sitter     │◀─────────────── │   → emit graph payload     │
│    fallback        │                 └───────────────────────────┘
└────────────────────┘
        │
        ▼
     Neo4j
```

### 2.2 Integration decision: **subprocess to a compiled extractor**

The Python backend shells out to a **pre-compiled .NET executable**. The only
contract between the two is:

- **Input:** CLI arguments (solution/repo path, output dir, scan id, options).
- **Bulk output → files:** the graph payload is written to the output dir
  (JSON / JSONL). Large graphs must **not** be streamed through stdout as one
  blob.
- **Summary → stdout:** a small JSON manifest
  (`{scan_id, analysis_mode, counts, output_files, errors}`).
- **Diagnostics → stderr.**
- **Exit code:** `0` = success, non-zero = failure. Python decides fallback
  based on exit code + analysis mode in the manifest.

Rationale: process isolation (an OOM on a giant solution doesn't take down the
API), language decoupling (Python never touches the CLR), and a swappable
black-box extractor.

### 2.3 Runtime / OS requirement

- The extractor machine needs the **.NET SDK** (MSBuild) — required by
  `MSBuildWorkspace` to open/compile `.csproj`. The SDK is cross-platform.
- **Windows is required only if the target project itself needs it** — i.e.
  legacy .NET Framework (`net48`, `packages.config`, non-SDK csproj) or
  Windows-only frameworks (WPF/WinForms, `net*-windows`). Modern SDK-style /
  cross-platform projects run fine on Linux containers.
- Publish self-contained + single-file per target RID
  (`-r linux-x64|win-x64 --self-contained -p:PublishSingleFile=true`). This
  removes the runtime dependency for *starting* the exe, but **not** the SDK
  dependency for MSBuild.

---

## 3. Core Principles (Rules)

1. **Analyze per project / compilation, once per project.** Build one
   `Compilation` per project (per compilation variant) so call resolution is
   reliable. Do not resolve calls file-by-file in isolation.

2. **Roslyn is authoritative for C#.** Roslyn is the priority engine.
   Tree-sitter is the **fallback only** and is **already implemented** — do not
   build it here. Every output must declare its **analysis mode**:
   - `semantic` — full Compilation + SemanticModel.
   - `partial-semantic` — some projects/compilations succeeded, others degraded.
   - `syntax-only` — syntax trees parsed, no semantic resolution.
   - `failed` — extraction could not produce usable output (Python may then
     invoke the tree-sitter fallback).

3. **Preserve uncertainty; never guess.** When a target can't be determined,
   emit an `UnresolvedReference` / `Dynamic` relation carrying **source
   evidence** (file + span), rather than fabricating an edge or dropping it
   silently.

4. **Separate compile-time target from runtime dispatch.** Roslyn knows the
   compile-time member; it generally cannot prove the single runtime
   implementation. Represent this explicitly:
   ```
   caller -[:CALLS_INTERFACE]-> IOrderService.Submit
   IOrderService.Submit -[:MAY_DISPATCH_TO]-> OrderService.Submit
   IOrderService.Submit -[:MAY_DISPATCH_TO]-> MockOrderService.Submit
   ```
   (`MAY_DISPATCH_TO` points from the interface/virtual member to each concrete
   implementation — see §7 note; the self-loop in the original sketch was a
   typo.)

5. **Compilation context is part of identity and evidence.** Retain at minimum:
   project path, assembly name, target framework, configuration, language
   version, nullable context, preprocessor symbols. The *same* source file can
   yield *different* active syntax under different TFMs/configs, so identity and
   evidence must be able to distinguish them.

---

## 4. The Four Stages

### Stage 1 — Walk (inventory)

Produce **two related inventories**:

- **Repository inventory** — use git where possible:
  `git -C <root> ls-files -z`. Create `File` nodes for relevant code, project,
  build, configuration, and documentation files. (Falls back to filesystem walk
  if not a git repo.)
- **Compilation inventory** — use MSBuild/Roslyn to determine which documents
  belong to which project and **compilation variant**.

Concepts created: `Repository`, `Solution`, `Project`, `CompilationVariant`,
`File`, `GeneratedFile`.

Relationships:
```
Repository        -[:CONTAINS]->        Project
Project           -[:COMPILES_AS]->     CompilationVariant
CompilationVariant-[:COMPILES]->        File
CompilationVariant-[:GENERATES]->       GeneratedFile
```

Important inputs to account for: `.sln` and `.csproj`; `Directory.Build.props` /
`Directory.Build.targets`; imported `.props` / `.targets`; project & assembly
references; conditional `Compile Include` / `Compile Remove` items; configuration
& target framework; preprocessor symbols; generated documents.

> **Realism note — multi-targeting.** A project with
> `<TargetFrameworks>net8.0;net48</TargetFrameworks>` produces **one
> `CompilationVariant` per TFM**. Decide up front whether the feature analyzes
> **all** variants or a **single chosen** TFM (recommended default: analyze one
> primary TFM per project, configurable, to keep graph size and runtime
> bounded). Full cross-product of TFM × configuration is rarely worth it —
> start with one variant per project.

### Stage 2 — Parse

Use Roslyn **syntax trees** for C# declarations, tokens, source spans,
attributes, directives, and malformed-code diagnostics.

- Conditional compilation (`#if`) must be tied to a **concrete compilation
  variant** — parse with that variant's `ParseOptions` (preprocessor symbols,
  language version), because active syntax differs per variant.

### Stage 3 — Resolve

Use:
- `Compilation` — project-level symbols and references.
- `SemanticModel.GetDeclaredSymbol()` — declarations.
- `SemanticModel.GetOperation()` / `IInvocationOperation` — calls (prefer the
  operation API over raw `GetSymbolInfo` on invocation syntax; it handles more
  cases cleanly).
- `IMethodSymbol`, `INamedTypeSymbol`, and related symbol interfaces.
- `OriginalDefinition` and `ReducedFrom` — normalize generics and extension
  methods (so `List<T>.Add` / reduced extension calls collapse to their
  definitions).
- `GetSymbolInfo()` + candidate symbols — for unresolved / ambiguous calls.
- `FindImplementationForInterfaceMember()` (or an indexed equivalent) —
  implementation edges.

**Resolution states (explicit, per relation):**
`Resolved`, `Ambiguous`, `External`, `VirtualDispatch`, `InterfaceDispatch`,
`Dynamic`, `ReflectionBased`, `BuildUnavailable`, `ParseError`, `Unresolved`.

### Stage 4 — Write

Persist **stable symbol nodes** and **evidence-rich relationships**.

- **Every node and edge carries:** `repo_id`, `scan_id`, `analysis_mode`,
  `project_key`, `target_framework`, `configuration`, `last_seen_scan_id`.
- **Every semantic edge carries source evidence:** `file_path`,
  `start/end line`, `start/end column`, `resolution_engine`,
  `resolution_status`, `dispatch_kind`.
- **Incremental reconciliation:** after a successful scan, remove nodes and edges
  **not seen in the current scan** (stale-sweep by `scan_id` /
  `last_seen_scan_id`).

> **Realism note — stale sweep.** Only sweep on a **successful, complete** scan
> (`analysis_mode = semantic`, exit 0). A `partial-semantic` or `failed` scan
> must **not** delete unseen nodes, or a transient build break will silently
> wipe large parts of the graph. Scope the sweep to what was actually
> (re)analyzed — e.g. per project/compilation — not the whole repo when only a
> subset was scanned.

---

## 5. Graph Contract (language-neutral)

One contract between analyzer(s) and the writer. Keep it language-neutral so the
tree-sitter fallback can emit the same shape.

### 5.1 Top-level payload

```json
{
  "schema_version": 1,
  "repo_id": "orders-service",
  "scan_id": "c7d3...",
  "analysis_mode": "semantic",
  "compilations": [],
  "entities": [],
  "relations": [],
  "diagnostics": []
}
```

### 5.2 Entity

```json
{
  "id": "sym:7A5E...",
  "kind": "Method",
  "name": "Submit",
  "canonical_name": "M:Acme.Orders.OrderService.Submit(Acme.Orders.Order)",
  "file_path": "src/Orders/OrderService.cs",
  "start_line": 42,
  "end_line": 58,
  "metadata": {
    "language": "csharp",
    "project_key": "src/Orders/Orders.csproj",
    "assembly_name": "Acme.Orders",
    "target_framework": "net8.0",
    "generated": false,
    "external": false,
    "accessibility": "Public",
    "is_static": false,
    "is_abstract": false
  }
}
```

**Identity (`id`) guidance:** derive stable ids from the Roslyn
**documentation comment id** (`ISymbol.GetDocumentationCommentId()`, e.g.
`M:Acme.Orders.OrderService.Submit(Acme.Orders.Order)`) — that is what
`canonical_name` holds. Because the same symbol can exist in multiple
compilation variants, the Neo4j **node key** should be
`canonical_name` **+ discriminating compilation context** (at minimum
`project_key` + `target_framework`) unless you deliberately want to merge
variants. Pick one policy and document it in the writer.

### 5.3 Relation

```json
{
  "source": "sym:CALLER",
  "target": "sym:TARGET",
  "kind": "CALLS_INTERFACE",
  "resolved": true,
  "evidence": {
    "file_path": "src/Api/OrdersController.cs",
    "start_line": 51,
    "start_column": 17,
    "end_line": 51,
    "end_column": 39
  },
  "metadata": {
    "resolution_engine": "roslyn",
    "resolution_status": "InterfaceDispatch",
    "project_key": "src/Api/Api.csproj",
    "target_framework": "net8.0"
  }
}
```

> **Realism note — output format.** For a *heavy* repo, prefer **JSONL**
> (one entity/relation per line, or separate `entities.jsonl` /
> `relations.jsonl` files) over a single giant JSON array. It streams, it lets
> Python ingest into Neo4j incrementally, and it avoids loading the whole graph
> into memory on either side. The top-level payload above then becomes a small
> `manifest.json` pointing at the JSONL files.

---

## 6. Entity Kinds

```
Repository  Solution  Project  CompilationVariant  File  GeneratedFile
Namespace   Class     Record   Struct  Interface   Enum  Delegate
Method      Constructor  Property  Field  Event
Parameter   LocalFunction
Route
ExternalType  ExternalMethod
UnresolvedReference
```

**Prioritization / realism:**
- **Phase 1 (core graph):** `Repository`, `Project`, `CompilationVariant`,
  `File`, `Namespace`, `Class/Record/Struct/Interface/Enum`, `Method`,
  `Constructor`, `Property`, `Field`, `ExternalType`, `ExternalMethod`,
  `UnresolvedReference`.
- **`Parameter` — skip as a node in Phase 1.** Parameters massively inflate node
  count on a heavy repo for little graph-analysis value; keep them as method
  metadata (signature string / arity). Promote to nodes only if a concrete
  query needs it.
- **`LocalFunction`, `Event`, `Delegate`** — lower priority; add when needed.
- **`Route`** — ASP.NET endpoint detection (controllers / minimal APIs /
  attributes). Useful but non-trivial; treat as **Phase 2**.

---

## 7. Relation Kinds

```
Structural : CONTAINS  DECLARES  COMPILES  REFERENCES_PROJECT  REFERENCES_ASSEMBLY
Type model : INHERITS  IMPLEMENTS  OVERRIDES  IMPLEMENTS_MEMBER
Calls      : CALLS_DIRECT  CALLS_VIRTUAL  CALLS_INTERFACE  MAY_DISPATCH_TO
Data/other : CREATES  READS  WRITES  ROUTE_HANDLED_BY  DEPENDS_ON  RESOLVES_TO
```

**Prioritization / realism:**
- **Phase 1 (core):** `CONTAINS`, `DECLARES`, `COMPILES`, `REFERENCES_PROJECT`,
  `REFERENCES_ASSEMBLY`, `INHERITS`, `IMPLEMENTS`, `OVERRIDES`,
  `IMPLEMENTS_MEMBER`, `CALLS_DIRECT`, `CALLS_VIRTUAL`, `CALLS_INTERFACE`,
  `CREATES`, `RESOLVES_TO`.
- **`MAY_DISPATCH_TO` — keep, but it's the expensive one.** Enumerating concrete
  implementations of an interface/virtual member is a **whole-compilation**
  operation (`SymbolFinder.FindImplementationsAsync` / derived-type search).
  Direction: **interface/virtual member → each concrete implementation**. Bound
  its cost (e.g. only for members actually called, only within analyzed
  projects) or it can explode on a large solution. Ship it after core calls
  work.
- **`READS` / `WRITES` (field/property access) — Phase 2, off by default.** These
  edges are extremely high-volume on real code and can dwarf the call graph.
  Make them an opt-in flag.
- **`ROUTE_HANDLED_BY`** — pairs with the `Route` entity; Phase 2.
- **`DEPENDS_ON`** — treat as a **derived/aggregate** edge (roll-up of
  finer-grained relations), computed in Neo4j or a post-pass, not emitted
  per-call.

---

## 8. Compilation Context (identity + evidence fields)

Captured once per `CompilationVariant`, attached to entities/edges via
`project_key` + variant discriminators:

| Field | Source |
|---|---|
| `project_path` / `project_key` | `.csproj` path (repo-relative) |
| `assembly_name` | `Compilation.AssemblyName` |
| `target_framework` | project TFM |
| `configuration` | Debug/Release |
| `language_version` | `CSharpParseOptions.LanguageVersion` |
| `nullable_context` | project nullable setting |
| `preprocessor_symbols` | `ParseOptions.PreprocessorSymbolNames` |

---

## 9. Incremental Scans & Reconciliation

- Each run has a unique `scan_id`. Nodes/edges get `last_seen_scan_id` updated on
  every scan that observes them (`MERGE ... SET last_seen_scan_id = $scan_id`).
- **Stale sweep** runs only after a complete, successful scan (see §4 Stage 4
  realism note): delete nodes/edges within the analyzed scope whose
  `last_seen_scan_id != current scan_id`.
- **Neo4j writer must:** create a **uniqueness constraint** on the node key
  before loading (MERGE on a large graph without an index is unusably slow); use
  `UNWIND` + batched parameters; ingest from JSONL.

---

## 10. Analysis Mode & Fallback Flow (Python side)

```
run extractor (subprocess)
  ├─ exit 0 & mode == "semantic"          → ingest, then stale-sweep
  ├─ exit 0 & mode == "partial-semantic"  → ingest, NO full sweep (scope-limited)
  ├─ mode == "syntax-only"                → ingest with reduced trust, no sweep
  └─ exit != 0 / mode == "failed"         → invoke existing tree-sitter fallback
```

Roslyn stays authoritative; tree-sitter only runs when Roslyn cannot produce
usable semantic output.

---

## 11. Summary of What We Skip / Defer (explicitly)

| Item | Decision |
|---|---|
| Re-implement tree-sitter | **Skip** — already exists; Roslyn is primary, it's the fallback |
| `Parameter` as graph nodes | **Skip Phase 1** — keep as method metadata |
| `READS` / `WRITES` edges | **Defer / opt-in** — volume explosion risk |
| `Route` / `ROUTE_HANDLED_BY` | **Defer to Phase 2** |
| Full TFM × config cross-product | **Skip** — one primary variant per project by default |
| `MAY_DISPATCH_TO` | **Keep but Phase 2** — bound its cost |
| `DEPENDS_ON` | **Derive** as a roll-up, don't emit per-call |
| Single giant JSON array output | **Replace with JSONL** for heavy repos |
| In-process `pythonnet` bridge | **Rejected** — subprocess isolation chosen |

---

## 12. Suggested Build Order

1. C# extractor skeleton: `MSBuildLocator` → `MSBuildWorkspace` → open
   solution → per-project `Compilation`. Emit `Project`/`CompilationVariant`/
   `File` inventory + manifest. Prove the subprocess contract with Python.
2. Declarations: types + methods + `DECLARES`/`CONTAINS`, with canonical ids and
   compilation context.
3. Calls: `CALLS_DIRECT` / `CALLS_VIRTUAL` / `CALLS_INTERFACE` via
   `IInvocationOperation`, with evidence + resolution status; `Unresolved` /
   `Dynamic` / `External` handled explicitly.
4. Type model: `INHERITS` / `IMPLEMENTS` / `OVERRIDES` / `IMPLEMENTS_MEMBER`.
5. Neo4j writer: constraints, JSONL ingest, `last_seen_scan_id`, guarded stale
   sweep.
6. Phase 2: `MAY_DISPATCH_TO`, routes, READS/WRITES (opt-in).
```
