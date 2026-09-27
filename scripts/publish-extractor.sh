#!/usr/bin/env bash
# Publish the extractor for deployment.
#
#   scripts/publish-extractor.sh [linux-x64|win-x64] [output-dir]
#
# Self-contained to a FOLDER, deliberately not single-file: MSBuildWorkspace
# starts MSBuild in a separate BuildHost process and finds it relative to the
# assembly's own directory, which a single-file bundle leaves empty. A
# single-file build therefore publishes fine and then fails every project load at
# run time, so the csproj refuses it outright.
#
# Self-contained removes the need for a .NET *runtime* on the target machine. It
# does NOT remove the need for the .NET *SDK*: MSBuildWorkspace requires MSBuild
# to evaluate .csproj files, and the extractor exits 3 with a clear message when
# it cannot find one.
set -euo pipefail

RID="${1:-linux-x64}"
OUTPUT="${2:-artifacts/extractor-$RID}"

cd "$(dirname "$0")/.."
source scripts/dotnet-env.sh

dotnet publish extractor/src/CodeGraphify.Extractor \
  --configuration Release \
  --runtime "$RID" \
  --self-contained true \
  --output "$OUTPUT" \
  --nologo

echo
echo "Published to $OUTPUT"
echo "Point the backend at it with CODEGRAPHIFY_EXTRACTOR=$OUTPUT/codegraphify-extractor"
echo "The target machine still needs the .NET SDK for MSBuild."
