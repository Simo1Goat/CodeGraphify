#!/usr/bin/env bash
# Source this to put the locally installed .NET SDK on PATH:
#   source scripts/dotnet-env.sh
export DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"
export PATH="$DOTNET_ROOT:$PATH"
# Keep first-run noise out of CI logs and scan output.
export DOTNET_NOLOGO=1
export DOTNET_CLI_TELEMETRY_OPTOUT=1
