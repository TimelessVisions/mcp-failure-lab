# Replay this reproduction. Portable: resolves its own directory, so the
# package can be moved anywhere and still runs. Requires PowerShell 7+.
$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
uv run mcp-failure-lab replay (Join-Path $ScriptDir "repro.yaml")
