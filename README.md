# CodeGraphify

Parses a .NET codebase into a dependency / call graph and ingests it into Neo4j.
C# analysis is done by Roslyn (semantic, authoritative); a tree-sitter engine is
the fallback. See [context/csharp-extractor-context.md](context/csharp-extractor-context.md)
for the design this implements.

## Status

| Stage | State |
|---|---|
| Preflight probe (what kind of codebase is this?) | done |
| Stage 1 — Walk (repository + compilation inventory) | done |
| Stage 2 — Parse / declarations | done |
| Stage 3 — Resolve / calls | done |
| Stage 4 — Type model | done |
| Stage 5 — Write (Neo4j) | done |

## Layout

```
app/                  Python backend (a library; it does not own an app)
  contract/           the graph contract (source of truth) + identity policy
  preflight.py        classify a .NET codebase without MSBuild
  extractor_runner.py subprocess bridge and the fallback decision
  fallback.py         the seam the tree-sitter engine plugs into
extractor/            the Roslyn extractor (.NET 8 console app)
  fixtures/           SampleSolution (modern) and LegacySolution (net48/WPF)
contract/schema/      JSON Schema generated from app/contract/models.py
docs/INTEGRATION.md   how to merge this into the backend
scripts/              environment, publish and schema helpers
tests/                contract, inventory, degradation and decision-table tests
```

## Setup

The extractor needs the **.NET SDK** — MSBuild, not just the runtime, because
`MSBuildWorkspace` evaluates `.csproj` files. It is installed here under
`~/.dotnet`:

```bash
source scripts/dotnet-env.sh   # puts the local SDK on PATH
dotnet --version               # 8.0.425
```

Python dependencies (this repo uses `uv`; the venv has no `pip`):

```bash
uv pip install -e '.[dev]'
```

## Check a codebase before scanning it

Answers "can this host analyze this repo at all" in milliseconds, with no SDK, no
restore and no MSBuild — so it works on a codebase the extractor cannot open:

```bash
.venv/bin/python -m app.preflight /path/to/codebase --per-project
# exit 0 linux-ok · 1 mixed · 2 needs-windows · 3 unknown
```

It reports project style (SDK-style or legacy non-SDK), target frameworks
(normalizing `v4.8` to `net48`), whether Windows is required and why, and whether
each project has been restored. Worth running first: a `.NET Framework` or
WPF/WinForms project cannot resolve its references on Linux, and that shows up as a
verdict here rather than as a mysteriously empty graph later.

## Run a scan

```bash
source scripts/dotnet-env.sh
dotnet build extractor/src/CodeGraphify.Extractor

extractor/src/CodeGraphify.Extractor/bin/Debug/net8.0/codegraphify-extractor \
  --input path/to/Your.sln \
  --output /tmp/scan \
  --repo-id your-repo \
  --scan-id "$(uuidgen)" \
  --verbose
```

Or from Python:

```python
from app.extractor_runner import run_extractor

result = run_extractor(
    input_path="path/to/Your.sln", output_dir="/tmp/scan", repo_id="your-repo"
)
if result.may_sweep:       # only ever true for a complete semantic scan
    ...
```

## Ingest into Neo4j

```python
from app.extractor_runner import run_extractor
from app.neo4j_writer import Neo4jWriter

result = run_extractor(input_path="Your.sln", output_dir="/tmp/scan", repo_id="your-repo")

with Neo4jWriter.connect() as writer:
    writer.apply_schema()          # idempotent; required before a load
    report = writer.ingest(result) # sweeps only if the scan qualifies
    print(report.summary())
```

Connection comes from `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`,
defaulting to the local `docker compose` instance.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

They build the fixture solution and run real scans, so they need the SDK. They
skip rather than fail when it is missing.

## Validated on a real codebase

Scanned [eShopOnWeb](https://github.com/dotnet-architecture/eShopOnWeb) (Microsoft's
ASP.NET reference application: 10 projects, 254 C# files) — `semantic`, 10/10
projects, no dangling edges, ~21s and ~390 MB peak, ingesting to 3,913 nodes and
15,530 relationships in ~18s cold / ~5s warm. A second scan of unchanged source
swept **0 nodes and 0 relationships**, which is the id-stability property the whole
sweep design rests on.

Five real defects came out of that exercise, none of which the fixture could have
surfaced:

| Found | Why it mattered |
|---|---|
| Target framework read only from `.csproj` | eShopOnWeb declares it in `Directory.Packages.props`. With no framework found, the Windows checks had nothing to match, so **a Windows-only repo that centralises its TFM was reported `linux-ok`** |
| NuGet advisories (`NU1902/3`) treated as MSBuild failures | Degraded 3 healthy projects → `partial-semantic` → **sweep withheld on any repo containing a flagged package** |
| Source generators never run | See below — 60% of the call graph was missing |
| Generated trees not inventoried | 63 dangling `DECLARES` edges once generators started running |
| Generator output duplicated | `CS0757`/`CS0121`: every generated partial implemented twice, every call to one ambiguous |

### Source generators

MSBuildWorkspace loads a project's generator references but never runs them:
`GetCompilationAsync` returns only hand-written files, and
`GetSourceGeneratedDocumentsAsync` returns nothing. On eShopOnWeb that was eleven
generators loaded and zero documents produced.

Every type declared in a `.razor` or `.cshtml` file is generator output, so this is
not a corner case — running them changed the scan from `partial-semantic` to
`semantic` and took the call count from 2,800 to **6,963**, with 4,030 calls coming
from 386 members declared in generated views.

Two things were needed to make it work. The generators need the MSBuild-provided
configuration, which MSBuild writes to a generated `.editorconfig` that the
workspace loads — so it is parsed and handed back to them. And the Razor SDK ships
a global config (`RazorSourceGenerator.razorencconfig`) setting
`build_property.SuppressRazorSourceGenerator = true`, which exists so an **IDE** can
stand the generator down because it runs Razor itself. We are not an IDE, so that
one property is overridden.

## Decisions taken

These were open in the context document. Each is implemented in one place.

**Node identity** — [app/contract/identity.py](app/contract/identity.py), mirrored
in [EntityId.cs](extractor/src/CodeGraphify.Extractor/Symbols/EntityId.cs) and
held to that by `tests/test_identity_parity.py`.

- A symbol in an analyzed project is keyed by documentation comment id **plus
  project and target framework**, so the `net8.0` and `net48` builds of the same
  method are separate nodes — `#if` can give them different bodies.
- A symbol from a referenced assembly is keyed by **assembly plus documentation
  comment id only**, so every project referencing `System.String` points at one
  shared node. This is what makes "who uses this library" answerable.
- Namespaces are keyed by name alone: a naming scope, not a compiled artifact.
- `IDENTITY_VERSION` guards the scheme; bumping it forces a full re-scan.

**Edge granularity** — one edge per call site, with the evidence span in the
dedup key. The contract requires location evidence on every semantic edge, which
rules out collapsing to a per-pair count.

**Target frameworks** — one primary variant per project, chosen by the
`--tfm-preference` order (default newest-first). `--all-target-frameworks` emits
every variant. The fixture proves this matters: `LegacyShim.cs` is compiled only
into the `netstandard2.0` variant of the same project.

**Degraded scans** — a project is degraded when it produces missing-reference
errors (`CS0246`, `CS0234`, `CS0012`, `CS0400`), which in practice means packages
were never restored. One degraded project makes the whole scan
`partial-semantic`, so the stale sweep is refused. Ordinary compile errors do not
degrade a scan: Roslyn still resolves most symbols in a file that fails to
compile.

**Configuration** — `Debug` by default, and part of a variant's identity, because
it changes `DefineConstants` and so changes which syntax is active.

**Declaration walk** — the *symbol* tree (`compilation.Assembly.GlobalNamespace`),
not the syntax trees. Both find the same declarations, but the symbol tree visits
each symbol once, so a partial class spread over several files is naturally one
node with several declaring references rather than two nodes to merge afterwards.

**Two containment edges, meaning different things:**

- `CONTAINS` — lexical nesting: namespace in namespace, type in namespace, nested
  type in type.
- `DECLARES` — a declaration site: the type that declares a member, and the file a
  type is declared in. A partial type gets one `DECLARES` edge per file, which is
  how "where is this class defined" stays answerable when `file_path` can only
  name one.

**What is deliberately not a node** — property and event accessors (the `Property`
or `Event` node represents them; emitting both doubles the member count and splits
calls across two nodes), property backing fields, and parameters. Methods carry
`signature`, `parameter_types` and arity as metadata instead.

**What is a node despite being synthesized** — a record's positional properties.
They are implicitly declared, so a blanket skip of synthesized members would drop
the entire public surface of every positional record.

**Calls are driven by the operation API** (`IInvocationOperation`), not by
`GetSymbolInfo` on invocation syntax. The operation tree separates cases that look
identical in source — a dynamic invocation, a delegate invoke, a reduced extension
call, an invalid call with candidates — so classification rests on what Roslyn
proved rather than on what the code looks like.

**Two fields describe every call edge, answering different questions:**

- `dispatch_kind` — how the call reaches its target: `Direct`, `Virtual`,
  `Interface`, `Dynamic`, `Reflection`, `None`.
- `resolution_status` — how confidently the target was identified: `Resolved`,
  `External`, `VirtualDispatch`, `InterfaceDispatch`, `Dynamic`, `ReflectionBased`,
  `Ambiguous`, `Unresolved`, `ParseError`.

A virtual call into a referenced assembly is `External` + `Virtual`; one field
cannot hold both.

**External means "not in any analyzed project"**, not "not in this compilation".
A call from one project into another crosses a compilation boundary while staying
inside the codebase, and it must land on that project's own node rather than a
duplicate — otherwise the graph fragments exactly where cross-project dependency
analysis matters.

**Normalization** — `ReducedFrom` for extension methods and `OriginalDefinition`
for generics, so `Repository<Order>.Add` collapses to `Repository<T>.Add`. The
constructed form is kept in `constructed_target` so the concrete instantiation is
not lost.

**Attribution** — a call inside a lambda or local function belongs to the member
containing it, since neither is a node in phase 1. A call from a property body
belongs to the property, matching stage 2's choice not to emit accessors.

**The type model records declared relationships, not their closure.** Only
directly declared interfaces get `IMPLEMENTS`; the transitive set is what a graph
query computes, and storing it would both multiply edges and make "which type
actually declares this" unanswerable. An interface's base interfaces are `INHERITS`,
because an interface extends rather than implements.

**Universal relationships are left out.** No `INHERITS` to `object`, `ValueType`,
`Enum`, `Delegate` or `Array`, and no `OVERRIDES` of `object.ToString`/`Equals`/
`GetHashCode` — every type has these, they carry no dependency information, and
including one while skipping the other would be incoherent. Deriving from a
framework type that is *not* universal (say `InvalidOperationException`) is a real
dependency and is recorded.

**`IMPLEMENTS_MEMBER` uses `FindImplementationForInterfaceMember`,** not name
matching: an explicit implementation is named
`Acme.Core.Orders.IOrderService.Submit`, so a name match would miss it while also
cross-wiring overloads. It walks `AllInterfaces` so interfaces inherited through a
base interface are covered, but emits only when the implementation is declared by
*that* type — otherwise the same edge is re-emitted once per derived type, growing
quadratically down a deep hierarchy.

**Two additions to the contract's vocabulary**, both to avoid asserting something
false:

- `CALLS_UNRESOLVED` — a call whose target cannot be named (dynamic, reflective,
  ambiguous, broken), pointing at an `UnresolvedReference` that carries the
  evidence. None of the `CALLS_*` kinds could be used without claiming a dispatch
  that was never proved. Context §3.3 asks for such a relation.
- `ExternalAssembly` — a referenced assembly as a whole, separate from
  `ExternalType` for a type inside one, so "which libraries do we depend on" and
  "which of their types do we touch" stay distinct.

## Deployment notes

**Do not publish single-file.** `MSBuildWorkspace` starts MSBuild in a separate
BuildHost process and locates it relative to the assembly's own directory, which
a single-file bundle leaves empty; every project load then fails with
`ArgumentNullException`. Verified here, and the csproj now refuses such a publish
with an explanatory error. Use:

```bash
scripts/publish-extractor.sh linux-x64
```

Self-contained removes the need for a .NET **runtime** on the target machine. It
does not remove the need for the .NET **SDK**: without one the extractor exits 3
with a clear message rather than producing a misleading empty graph.

**Windows is only needed if the target codebase needs it** — legacy .NET
Framework (`net48`, `packages.config`, non-SDK csproj) or Windows-only frameworks
(WPF/WinForms, `net*-windows`). SDK-style cross-platform projects analyze fine on
Linux.

## Subprocess contract

| Channel | Carries |
|---|---|
| CLI args | input, output dir, repo id, scan id, options |
| Output dir | `entities.jsonl`, `relations.jsonl`, `compilations.jsonl`, `diagnostics.jsonl`, `manifest.json` |
| stdout | the manifest, and nothing else |
| stderr | progress and diagnostics |
| exit code | 0 usable · 2 bad arguments · 3 no SDK · 4 failed |

JSONL rather than one JSON array so a heavy repo streams through without either
side holding the whole graph in memory.

The mode drives what the caller may do — `app/extractor_runner.py`, one table:

| exit | mode | outcome |
|---|---|---|
| 0 | `semantic` | ingest, then stale-sweep |
| 0 | `partial-semantic` | ingest, no sweep |
| 0 | `syntax-only` | ingest with reduced trust, no sweep |
| ≠0 | `failed` / unparseable | fall back to tree-sitter |

## The writer

**Labels.** Every node carries `:CodeEntity` plus a label for its kind. The shared
label is what the uniqueness constraint and the sweep work against, so both are one
indexed operation; the kind label makes the graph readable and lets queries narrow
cheaply.

Neo4j 5.26 cannot parameterise a label or relationship type, so kinds are
interpolated into Cypher — and therefore validated against the contract enums
first. That check is the only thing between the payload and Cypher injection, so
nothing else may build a statement from a kind.

**Identity.** Nodes MERGE on `id`; relationships MERGE on an `edge_key` that
includes the evidence span, so one call site is one edge and the same caller
calling the same target on two lines stays two facts. Relationship uniqueness
constraints are Enterprise-only, so identity rests on that key plus indexes that
keep the MERGE from scanning.

**Ingest is streamed and batched** from JSONL, grouped by kind, so neither the
process nor the driver ever holds the whole graph.

**A missing endpoint is counted, not silently dropped** — and never MERGE'd, which
would invent a blank node. A non-zero skip count means the extractor and the writer
disagree about identity, which is worth knowing loudly.

**Every scan leaves a `:Scan` node**, including a failed or partial one, so "why is
this project missing from the graph" still has an answer after the manifest's temp
directory is gone.

### The stale sweep

The only destructive operation in the system, so it is guarded in code rather than
just documented:

- It runs only for a complete `semantic` scan. Asking for it on any other kind
  **raises** — the guard exists for the caller who is sure, because being wrong
  deletes real data.
- It is scoped to the projects the scan actually analyzed. A scan of one project
  cannot delete another's graph, and an empty scope raises rather than meaning
  "everything".
- **Relationships are swept in their own right**, not only via `DETACH DELETE`. The
  commonest edit removes a call while leaving both methods in place, so the edge is
  stale while its endpoints are current.
- Nodes with no `project_key` — files, namespaces, referenced assemblies — are
  shared and left alone. Judging one stale would need proof no other project still
  uses it.
- Deletes are batched, so a sweep after a large refactor does not build one enormous
  transaction.

`DEPENDS_ON` is **derived** after the sweep, as context §7 asks, and reconciles its
own stale edges. It is excluded from the observation sweep: being recomputed every
scan, it is stale by scan id every time, and sweeping it would report phantom
deletions on an unchanged rescan.

## What the graph can answer now

Stages 1–4 together make dispatch answerable. A `CALLS_INTERFACE` edge gives the
compile-time target; following `IMPLEMENTS_MEMBER` backwards from it gives the
concrete members that could run:

```
RunAll --CALLS_INTERFACE--> IOrderService.Submit(Order)
                              <--IMPLEMENTS_MEMBER-- OrderService.Submit
                              <--IMPLEMENTS_MEMBER-- MockOrderService.Submit
                              <--IMPLEMENTS_MEMBER-- ExplicitOrderService....Submit
```

The two `Submit` overloads reach *different* implementation sets, which is the
clearest single illustration of why this needs semantic analysis: a name-based graph
gives both overloads all six implementations.

Phase 2's `MAY_DISPATCH_TO` is a materialization of that traversal rather than a new
analysis, which is why it was cheap to defer.

In Cypher, against the loaded graph:

```cypher
MATCH (caller:Method {name: 'RunAll'})-[call:CALLS_INTERFACE]->(contract:Method)
OPTIONAL MATCH (impl)-[:IMPLEMENTS_MEMBER]->(contract)
RETURN call.start_line, contract.signature, collect(DISTINCT impl.declaring_type)
```

Cross-project dependency, rolled up:

```cypher
MATCH (source:Project)-[d:DEPENDS_ON]->(target:Project)
RETURN source.name, target.name, d.weight
```

## Neo4j

```bash
docker compose up -d      # bolt on 7687, browser on 7474, neo4j/codegraphify
```

Verified working: Neo4j 5.26 community, Bolt reachable from the Python driver,
constraints creatable.

**Note on flaky networking here.** This machine has IPv6 enabled with no global
IPv6 connectivity (link-local and loopback only, no default route), and DNS is
intermittent. An image pull and the .NET SDK download each failed once with
`network is unreachable` or `Could not resolve host`, then succeeded unchanged on
retry. If a download fails, retry it before concluding anything is misconfigured;
`curl -4` forces IPv4 when a retry is not enough.

## Integration

The FastAPI backend and the tree-sitter analyzer live in a separate repository, to
be merged later. Everything here is therefore a library: nothing imports from the
backend and nothing constructs a `FastAPI` app. The seam the tree-sitter engine
plugs into is declared as a Protocol in [app/fallback.py](app/fallback.py) and
exercised with a stub in `tests/test_fallback_seam.py`.

See [docs/INTEGRATION.md](docs/INTEGRATION.md) for what each side provides and the
three places they meet.

## Degradation

The extractor must never report success on a codebase it could not resolve — a
false `semantic` would earn a stale sweep and delete the real graph. Verified
against `extractor/fixtures/LegacySolution`, which cannot be analyzed on Linux by
design:

- A compilation without a core library is detected directly (Roslyn returns an
  error symbol for `System.Object`) rather than by counting error codes, and marks
  the project degraded.
- Projects MSBuild failed to evaluate are excluded from the analyzed scope.
- Diagnostics say what to do (`restore`, or use a Windows host), not merely that
  something failed.
- A project MSBuild could not evaluate still gets a real target framework, read
  from the `.csproj`, because `unknown` in a node key would make two such projects
  collide.

## Open question

The target codebase is known only to be ".NET". If it is legacy .NET Framework or
WPF/WinForms it needs a Windows host; if it is SDK-style, a Linux container is
fine. Run the preflight probe against it to find out — that is what it is for.

## Operational notes

- **The first scan after an SDK or workload change can be ~50× slower** — 713s
  against 13s on the same solution, while MSBuild and NuGet caches rebuild. A short
  subprocess timeout would misread that as failure and fall back to tree-sitter for
  no reason; the default is an hour.
- **A degraded scan sometimes means "retry", not "the code is broken."** A transient
  network failure during restore produces missing references, which is correctly
  reported as `partial-semantic`. Worth distinguishing in the orchestration layer.
