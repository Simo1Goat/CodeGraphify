#!/usr/bin/env bash
#
# Scan a .NET codebase into a dependency graph and load it into Neo4j.
#
#   scripts/scan.sh                        # scans eShopOnWeb
#   scripts/scan.sh /path/to/Your.sln      # scans something else
#   scripts/scan.sh /path/to/Your.sln --all-target-frameworks
#
# Runs from ANY directory: it locates the repository from its own path and never
# depends on where you invoked it. Extra arguments are passed to python -m app.scan.
#
# Skip steps with environment variables when iterating:
#   SKIP_NEO4J=1 SKIP_RESTORE=1 SKIP_BUILD=1 scripts/scan.sh

set -euo pipefail

# --- Step 0: find ourselves, so no step depends on the current directory ------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

DEFAULT_TARGET="$HOME/codegraphify-testdata/eShopOnWeb/eShopOnWeb.sln"
NEO4J_CONTAINER="codegraphify-neo4j"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# --- Step 1: work out which solution to scan ---------------------------------
# A directory holding several .sln files is ambiguous, and picking one silently is
# how you end up scanning the wrong thing: eShopOnWeb ships both eShopOnWeb.sln and
# Everything.sln. So ask rather than guess.
TARGET="${1:-$DEFAULT_TARGET}"
[[ $# -gt 0 ]] && shift || true
TARGET="${TARGET/#\~/$HOME}"

if [[ -f "$TARGET" ]]; then
    SOLUTION="$TARGET"
elif [[ -d "$TARGET" ]]; then
    mapfile -t FOUND < <(find "$TARGET" -maxdepth 1 -name '*.sln' -o -maxdepth 1 -name '*.slnx' | sort)
    case "${#FOUND[@]}" in
        1) SOLUTION="${FOUND[0]}" ;;
        0) mapfile -t FOUND < <(find "$TARGET" -name '*.csproj' | sort)
           [[ "${#FOUND[@]}" -eq 1 ]] || die "no single solution or project under $TARGET; pass one explicitly"
           SOLUTION="${FOUND[0]}" ;;
        *) die "$TARGET holds several solutions; pass the one you want:
$(printf '  %s\n' "${FOUND[@]}")" ;;
    esac
else
    die "$TARGET does not exist"
fi

SOLUTION="$(cd -- "$(dirname -- "$SOLUTION")" && pwd)/$(basename -- "$SOLUTION")"
TARGET_ROOT="$(git -C "$(dirname -- "$SOLUTION")" rev-parse --show-toplevel 2>/dev/null \
               || dirname -- "$SOLUTION")"

say "scanning $SOLUTION"
echo "    repo root : $TARGET_ROOT"
echo "    tooling   : $REPO_ROOT"

# --- Step 2: put the .NET SDK on PATH ----------------------------------------
# MSBuildWorkspace needs the SDK, not just the runtime. Installed under ~/.dotnet,
# which is not on PATH by default.
say "step 2/6  .NET SDK"
# shellcheck source=/dev/null
source "$REPO_ROOT/scripts/dotnet-env.sh"
command -v dotnet >/dev/null || die "no dotnet on PATH; see the Setup section of README.md"
echo "    $(dotnet --version)"

# --- Step 3: make sure the Python side is installed --------------------------
# Checked rather than reinstalled every run. The venv has no pip, so uv does it.
say "step 3/6  Python dependencies"
PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || die "no virtualenv at $REPO_ROOT/.venv"
if ! "$PYTHON" -c 'import app.neo4j_writer' >/dev/null 2>&1; then
    UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
    [[ -x "$UV" ]] || die "uv not found; install it or run: $PYTHON -m pip install -e '.[dev]'"
    echo "    installing..."
    "$UV" pip install -q -e "$REPO_ROOT[dev]"
fi
echo "    ok"

# --- Step 4: start Neo4j and wait for it to accept connections ---------------
# Idempotent: fine if it is already running.
say "step 4/6  Neo4j"
if [[ -n "${SKIP_NEO4J:-}" ]]; then
    echo "    skipped (SKIP_NEO4J set)"
else
    docker compose up -d
    printf '    waiting for health'
    for _ in $(seq 1 60); do
        if [[ "$(docker inspect -f '{{.State.Health.Status}}' "$NEO4J_CONTAINER" 2>/dev/null)" == "healthy" ]]; then
            printf ' ok\n'; break
        fi
        printf '.'; sleep 2
    done
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$NEO4J_CONTAINER" 2>/dev/null)" == "healthy" ]] \
        || die "Neo4j did not become healthy; check 'docker compose logs neo4j'"
fi

# --- Step 5: build the extractor ---------------------------------------------
# Incremental, so this is cheap on repeat runs.
say "step 5/6  build the extractor"
if [[ -n "${SKIP_BUILD:-}" ]]; then
    echo "    skipped (SKIP_BUILD set)"
else
    dotnet build "$REPO_ROOT/extractor/src/CodeGraphify.Extractor" -v q --nologo
fi

# --- Step 6: restore the target, then scan and ingest it ---------------------
# Restore is not optional: without it Roslyn cannot resolve references, every
# affected project is reported as degraded, and the scan will not reconcile the
# graph. Workloads matter too -- Blazor WebAssembly and MAUI each need one.
say "step 6/6  restore the target codebase"
if [[ -n "${SKIP_RESTORE:-}" ]]; then
    echo "    skipped (SKIP_RESTORE set)"
else
    # NuGetAudit off: we are reading code, not auditing dependencies, and the
    # advisories are pure noise here.
    dotnet restore "$SOLUTION" -v q --nologo -p:NuGetAudit=false
    echo "    packages restored"

    # Workloads are advisory: many solutions need none, and this fails outright on a
    # solution containing a non-MSBuild project (a .dcproj, say). Output is kept in a
    # log rather than dumped, and a failure here is not fatal -- a genuinely missing
    # workload shows up as a degraded project with a diagnostic that says so.
    WORKLOAD_LOG="$(mktemp)"
    if dotnet workload restore "$SOLUTION" --skip-sign-check >"$WORKLOAD_LOG" 2>&1; then
        echo "    workloads restored"
    else
        echo "    workloads: nothing to do, or unavailable (log: $WORKLOAD_LOG)"
    fi
fi

# Preflight, extract and ingest. Whether the graph is reconciled is decided from
# the scan's analysis mode inside here, never asserted from this script.
"$PYTHON" -m app.scan \
    --input "$SOLUTION" \
    --repo-root "$TARGET_ROOT" \
    --repo-id "$(basename -- "$TARGET_ROOT")" \
    "$@"
