#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DOTNET_CLI_HOME="${DOTNET_CLI_HOME:-$ROOT/.dotnet-home}"
NUGET_PACKAGES="${NUGET_PACKAGES:-$ROOT/.nuget/packages}"
DOTNET_NOLOGO="${DOTNET_NOLOGO:-1}"
DOTNET_SKIP_FIRST_TIME_EXPERIENCE="${DOTNET_SKIP_FIRST_TIME_EXPERIENCE:-1}"
export DOTNET_CLI_HOME NUGET_PACKAGES DOTNET_NOLOGO DOTNET_SKIP_FIRST_TIME_EXPERIENCE
if command -v dotnet >/dev/null 2>&1; then
  dotnet restore
else
  echo "The .NET SDK 9.0 is required on the host. Install dotnet, then rerun this script." >&2
  exit 127
fi
