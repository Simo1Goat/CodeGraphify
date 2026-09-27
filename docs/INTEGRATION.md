# Merging this feature into the backend

Written for whoever merges this repository into the FastAPI backend that holds the
tree-sitter analyzer. It states exactly what this side provides, what it needs
from that side, and the three places the two have to meet.

The feature was built in a separate repository because the backend was not
available. It was built to be merged, so nothing here assumes it owns the
application.

## What this side provides

| Module | Role | Depends on the backend? |
|---|---|---|
| `app/contract/` | The graph contract: entities, relations, manifest, identity policy | no |
| `app/preflight.py` | Classifies a .NET codebase with no SDK, no MSBuild, no restore | no |
| `app/extractor_runner.py` | Runs the Roslyn extractor, classifies the result | no |
| `app/fallback.py` | The Protocol the tree-sitter engine must satisfy | no |
| `extractor/` | The Roslyn extractor (.NET 8) | no |

Nothing imports from the backend, and nothing constructs a `FastAPI` app. The
whole of `app/` is importable as a library.

## The three meeting points

### 1. The tree-sitter fallback

`app/fallback.py` declares a `FallbackAnalyzer` Protocol with one method. Wrap
whatever the backend already has:

```python
from app.fallback import FallbackRequest
from app.contract import Manifest

class TreeSitterAdapter:
    def __init__(self, existing_parser):
        self._parser = existing_parser

    def analyze(self, request: FallbackRequest) -> Manifest | None:
        # Write entities.jsonl, relations.jsonl and manifest.json into
        # request.output_dir, in the app.contract shape.
        ...
```

The Protocol is structural, so the adapter does not have to subclass anything. Two
obligations are enforced rather than trusted, by `validate_fallback_manifest`:

- `analysis_mode` must be `syntax-only` or `failed`. A syntax-only engine claiming
  `semantic` would earn a stale sweep it cannot justify, and delete the graph.
- `resolution_engine` must not be `roslyn`, so a mixed graph stays attributable
  edge by edge.

`tests/test_fallback_seam.py` exercises this with a stub. Point those tests at the
real adapter and they become the merge's acceptance criteria.

### 2. The HTTP surface

Not yet written, deliberately. When it is, it will be an `APIRouter` to mount, not
a `FastAPI` app:

```python
from app.api import router
app.include_router(router, prefix="/analysis")
```

Scans are long-running, so the endpoint starts a background job and returns a scan
id; it must not block a request thread. If the backend already has a job runner,
that router should use it rather than introduce a second one.

### 3. Configuration

Two things need to come from the backend's own config rather than from here:

- `CODEGRAPHIFY_EXTRACTOR` — path to the published extractor binary. Defaults to
  the local debug build, which is a development convenience, not a deployment.
- Neo4j connection details. `docker-compose.yml` here is for local development
  only; the backend presumably already has a driver and a connection to reuse.

## What the backend has to decide

**Where the extractor runs.** It needs the .NET **SDK**, not just the runtime,
because `MSBuildWorkspace` evaluates `.csproj` files. If the target codebase turns
out to need Windows (see below), the extractor has to run on a Windows host or
agent while the API stays where it is. The subprocess contract makes that
straightforward — the two only exchange CLI arguments and files — but it is an
infrastructure decision, not a code one.

**Whether to run preflight on every scan.** It costs milliseconds and no SDK, and
it answers "can this host analyze this repo at all" before anything expensive
starts:

```bash
.venv/bin/python -m app.preflight /path/to/codebase --per-project
# exit 0 linux-ok · 1 mixed · 2 needs-windows · 3 unknown
```

This matters for the codebase this feature targets, which is currently known only
to be ".NET". If it is legacy .NET Framework or WPF/WinForms, a Linux host cannot
resolve its references and every scan will come back degraded. The probe reports
that as a verdict instead of leaving it to be inferred from a wrecked graph.

**Whether to restore before scanning.** Unrestored projects are the most common
cause of a degraded scan, and Roslyn cannot tell "this package reference was never
restored" from "this code is broken". The probe reports restore state per project.

## What is not built yet

Stages 2 to 4 of the design: declarations, calls, the type model and the Neo4j
writer. The contract, the identity policy and the subprocess bridge are settled
and tested, so those stages add emitters behind an interface that already exists.

`Outcome.FALL_BACK` is currently a decision with no consumer: the runner
classifies a failed scan correctly, but there is nothing to hand it to until the
adapter above exists.
