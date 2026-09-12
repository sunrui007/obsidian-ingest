# Windows convenience launcher; Python users can call ingest.py directly.
$ErrorActionPreference = 'Stop'
$pythonBin = $env:OBSIDIAN_INGEST_PYTHON
if (-not $pythonBin) {
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundled) {
        $pythonBin = $bundled
    } else {
        $command = Get-Command python3, python -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { $pythonBin = $command.Source }
    }
}
if (-not $pythonBin) { throw 'Install Python 3.10+ or set OBSIDIAN_INGEST_PYTHON to its executable path.' }
& $pythonBin -X utf8 (Join-Path $PSScriptRoot 'ingest.py') @args
exit $LASTEXITCODE
