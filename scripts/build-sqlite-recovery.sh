#!/bin/sh
# Build the recovery helper from the same vendored source as the server VFS.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../../.." && pwd)
output=${1:-"$repo_root/meshagent-sdk/meshagent-cli/.venv/bin/meshagent-sqlite-recover"}
mkdir -p -- "$(dirname -- "$output")"
output_dir=$(CDPATH= cd -- "$(dirname -- "$output")" && pwd)
output="$output_dir/$(basename -- "$output")"
cd "$repo_root/third_party/juicefs/third_party/litestream"
go build -trimpath -o "$output" ./cmd/meshagent-sqlite-recover
"$output" --version
