param([string]$Entry = 'web/server.py')
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path $PSScriptRoot -Parent
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$oldPythonPath = $env:PYTHONPATH
Push-Location $projectDir
try {
    if (Test-Path -LiteralPath $bundledPython) {
        $pythonCommand = $bundledPython
        $localPackages = Join-Path $projectDir 'venv\Lib\site-packages'
        if (Test-Path -LiteralPath $localPackages) {
            $env:PYTHONPATH = $localPackages + [IO.Path]::PathSeparator + $oldPythonPath
        }
    } else {
        $pythonCommand = (Get-Command python -ErrorAction Stop).Source
    }
    & $pythonCommand -X utf8 $Entry @args
    $taskExitCode = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $oldPythonPath
    Pop-Location
}
exit $taskExitCode
