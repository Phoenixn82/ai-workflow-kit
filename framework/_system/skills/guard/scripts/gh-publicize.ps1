[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [string]$Repo
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Gate = Join-Path $ScriptDir "public_push_gate.py"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  throw "python is required to run the public push gate"
}

if (-not $Repo) {
  if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "gh is required to resolve the current repository"
  }
  $Repo = gh repo view --json nameWithOwner --jq ".nameWithOwner"
}

python $Gate --mode publicize --repo $Repo
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

gh repo edit $Repo --visibility public --accept-visibility-change-consequences
