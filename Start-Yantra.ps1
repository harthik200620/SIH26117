$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Python environment missing. On a connected setup machine run: uv sync --extra knowledge --extra vision --extra render. Then disconnect.'
}
& $taskPython scripts/run_workbench.py @args
