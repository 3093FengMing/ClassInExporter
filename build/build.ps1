[CmdletBinding()]
param(
    [switch]$SkipDependencyInstall,
    [switch]$VerifyOnly,
    [string]$OutputDirectory = 'dist'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$buildSourceDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$projectDirectory = (Resolve-Path -LiteralPath (Join-Path $buildSourceDirectory '..')).Path
$specPath = Join-Path $buildSourceDirectory 'ClassInExporter.spec'
$launcherPath = Join-Path $buildSourceDirectory 'launcher.py'
$venvDirectory = Join-Path $projectDirectory '.venv'
$pythonPath = Join-Path $venvDirectory 'Scripts\python.exe'
$workDirectory = Join-Path $projectDirectory '.build'
$distDirectory = [System.IO.Path]::GetFullPath((Join-Path $projectDirectory $OutputDirectory))
$bundleDirectory = Join-Path $distDirectory 'ClassInExporter'
$archivePath = Join-Path $distDirectory 'ClassInExporter-0.4.2-win64.zip'

function Invoke-BuildPython {
    param([string[]]$Arguments)
    & $pythonPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python build step failed with exit code $LASTEXITCODE."
    }
}

Push-Location -LiteralPath $projectDirectory
try {
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        if ($SkipDependencyInstall) {
            throw 'The local .venv is missing; run without -SkipDependencyInstall first.'
        }
        & py -m venv $venvDirectory
        if ($LASTEXITCODE -ne 0) {
            throw 'Install Python x64 to prepare the build environment.'
        }
    }
    if (-not $SkipDependencyInstall) {
        Invoke-BuildPython -Arguments @('-m', 'pip', 'install', '-r', 'requirements-build.txt')
    }
    $validateOutput = @'
from pathlib import Path
root = Path.cwd().resolve()
import sys
output = Path(sys.argv[1]).resolve()
for target in ((root / ".build").resolve(), output):
    if target == root or not target.is_relative_to(root):
        raise RuntimeError("Build output must stay inside the project directory")
if not output.is_relative_to((root / "dist").resolve()):
    raise RuntimeError("Portable output must stay inside dist")
exports = output / "ClassInExporter" / "exports"
if exports.exists():
    raise RuntimeError("Move the portable program's existing exports before rebuilding its directory")
'@
    Invoke-BuildPython -Arguments @('-X', 'utf8', '-c', $validateOutput, $distDirectory)
    New-Item -ItemType Directory -Path $workDirectory -Force | Out-Null
    $syntaxCheck = @'
import ast
from pathlib import Path
for source in (Path("build/ClassInExporter.spec"), Path("build/launcher.py")):
    ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
import PyInstaller
print("Build configuration syntax OK; PyInstaller " + PyInstaller.__version__)
'@
    Invoke-BuildPython -Arguments @('-X', 'utf8', '-c', $syntaxCheck)
    $sourceReport = Join-Path $workDirectory 'source-smoke-test.json'
    Invoke-BuildPython -Arguments @('-X', 'utf8', $launcherPath, '--self-test', '--self-test-output', $sourceReport)
    if ($VerifyOnly) {
        Write-Host "Build preflight passed: $sourceReport"
        return
    }

    Invoke-BuildPython -Arguments @('-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', $distDirectory, '--workpath', $workDirectory, $specPath)
    $executablePath = Join-Path $bundleDirectory 'ClassInExporter.exe'
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        throw 'PyInstaller did not produce ClassInExporter.exe.'
    }
    $frozenReport = Join-Path $workDirectory 'frozen-smoke-test.json'
    $smokeProcess = Start-Process -FilePath $executablePath -ArgumentList @('--self-test', '--self-test-output', ('"' + $frozenReport + '"')) -WindowStyle Hidden -Wait -PassThru
    if ($smokeProcess.ExitCode -ne 0) {
        throw "The portable executable smoke check failed; inspect $frozenReport."
    }
    $smokeResult = Get-Content -LiteralPath $frozenReport -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $smokeResult.ok -or -not $smokeResult.frozen) {
        throw "The portable executable smoke report is incomplete; inspect $frozenReport."
    }
    Copy-Item -LiteralPath (Join-Path $buildSourceDirectory 'portable-readme.txt') -Destination (Join-Path $bundleDirectory 'README.txt') -Force
    foreach ($noticeFile in @('LICENSE', 'NOTICE')) {
        Copy-Item -LiteralPath (Join-Path $projectDirectory $noticeFile) -Destination (Join-Path $bundleDirectory $noticeFile) -Force
    }

    # Package only the PyInstaller directory. No workspace cache, exports,
    # analysis evidence, account profiles or credentials are added to the ZIP.
    Compress-Archive -LiteralPath $bundleDirectory -DestinationPath $archivePath -CompressionLevel Optimal -Force
    $archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumPath = $archivePath + '.sha256'
    [System.IO.File]::WriteAllText($checksumPath, $archiveHash + '  ' + [System.IO.Path]::GetFileName($archivePath) + "`n", [System.Text.Encoding]::ASCII)
    Write-Host "Portable executable: $executablePath"
    Write-Host "Portable ZIP: $archivePath"
    Write-Host "SHA256: $archiveHash"
}
finally {
    Pop-Location
}
