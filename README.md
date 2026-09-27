# CodeGraphify

Parses a .NET codebase into a dependency / call graph and ingests it into Neo4j.
C# analysis is done by Roslyn (semantic, authoritative); a tree-sitter engine is
the fallback. See [context/csharp-extractor-context.md](context/csharp-extractor-context.md)
for the design this implements.

## Status

| Stage | State |
|---|---|
| Stage 1 — Walk (repository + compilation inventory) | done |
| Stage 2 — Parse / declarations | not started |
| Stage 3 — Resolve / calls | not started |
| Stage 4 — Write (Neo4j) | not started |

## Layout

```
app/                  Python backend
  contract/           the graph contract (source of truth) + identity policy
  extractor_runner.py subprocess bridge and the fallback decision
extractor/            the Roslyn extractor (.NET 8 console app)
  fixtures/           SampleSolution: the hard cases, used by every test
contract/schema/      JSON Schema generated from app/contract/models.py
scripts/              environment, publish and schema helpers
tests/                contract, inventory and decision-table tests
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

## Open questions

- Where does the existing FastAPI backend and tree-sitter fallback live? This
  repo contained only the context document, so the orchestration layer here is
  new and the fallback is not yet wired to anything.
- Is the codebase to be analyzed SDK-style or legacy .NET Framework? That decides
  Linux container versus Windows host.
