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
| Stage 3 — Resolve / calls | not started |
| Stage 4 — Write (Neo4j) | not started |

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

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

They build the fixture solution and run real scans, so they need the SDK. They
skip rather than fail when it is missing.

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

## Neo4j

```bash
docker compose up -d      # bolt on 7687, browser on 7474, neo4j/codegraphify
```

**Known issue on this machine:** the Docker daemon resolves the registry over
IPv6, which is unreachable here, so the image pull fails
(`network is unreachable`). The registry is reachable over IPv4. Fixing it means
setting daemon-level config (`/etc/docker/daemon.json`) as root, which has not
been done. The same IPv6 preference affects `curl`, hence `-4` in the setup steps.

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
