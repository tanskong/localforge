param(
    [Parameter(Mandatory=$true)][string]$HostName,
    [Parameter(Mandatory=$true)][string]$KeyPath,
    [string]$RuntimeRoot = (Join-Path $env:USERPROFILE 'AppData\Local\LocalForge'),
    [string]$Version = "latest"
)
$ErrorActionPreference = "Stop"
$sshArgs = @('-i', $KeyPath, '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes')
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$download = Join-Path $RuntimeRoot 'downloads'
New-Item -ItemType Directory -Force -Path $download | Out-Null
$manifestName = if ($Version -eq 'latest') { 'latest.json' } else { "manifest-$Version.json" }
$manifestPath = Join-Path $download $manifestName
$remoteManifest = if ($Version -eq 'latest') { '/opt/localforge/releases/latest.json' } else { "/opt/localforge/releases/$Version/manifest.json" }
& ssh @sshArgs $HostName "cat $remoteManifest" | Set-Content -LiteralPath $manifestPath -Encoding UTF8
if ($LASTEXITCODE -ne 0) { throw "manifest download failed: $remoteManifest" }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.product -ne 'localforge') { throw 'invalid release product' }
if ($Version -ne 'latest' -and $manifest.version -ne $Version) { throw 'manifest version mismatch' }
$releaseVersion = $manifest.version
$remote = "/opt/localforge/releases/$releaseVersion"
$wheel = Join-Path $download $manifest.filename
& scp @sshArgs "$HostName`:$remote/$($manifest.filename)" $wheel
if ($LASTEXITCODE -ne 0) { throw 'wheel download failed' }
$sha = [Security.Cryptography.SHA256]::Create()
$actual = [BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($wheel))).Replace('-', '').ToLowerInvariant()
$sha.Dispose()
if ($actual -ne $manifest.sha256.ToLowerInvariant()) { throw 'release SHA256 mismatch' }
$versionRoot = Join-Path (Join-Path $RuntimeRoot 'versions') $releaseVersion
$venv = Join-Path $versionRoot 'venv'
$python = Join-Path $venv 'Scripts\python.exe'
New-Item -ItemType Directory -Force -Path (Split-Path $venv) | Out-Null
if (-not (Test-Path -LiteralPath $python)) {
    & py -3 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}
& $python -m pip install --upgrade $wheel
if ($LASTEXITCODE -ne 0) { throw 'wheel installation failed' }
& $python (Join-Path $PSScriptRoot 'installed_smoke.py') --version $releaseVersion
if ($LASTEXITCODE -ne 0) { throw 'installed black-box smoke failed; current.txt was not changed' }
$bin = Join-Path $RuntimeRoot 'bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$launcher = Join-Path $bin 'localforge.cmd'
$launcherText = @'
@echo off
setlocal
set "ROOT=%~dp0.."
set "VERSION="
for /f "usebackq delims=" %%V in ("%ROOT%\current.txt") do set "VERSION=%%V"
if not defined VERSION (echo LocalForge current.txt is missing or empty 1>&2 & exit /b 2)
set "EXE=%ROOT%\versions\%VERSION%\venv\Scripts\localforge.exe"
if not exist "%EXE%" (echo LocalForge current version is unavailable: %VERSION% 1>&2 & exit /b 3)
endlocal & "%EXE%" %*
'@
Set-Content -LiteralPath $launcher -Value $launcherText -Encoding ASCII
$tray = Join-Path $bin 'localforge-tray.vbs'
$trayText = @'
Set shell = CreateObject("WScript.Shell")
root = "%ROOT%"
Set fso = CreateObject("Scripting.FileSystemObject")
Set f = fso.OpenTextFile(root & "\current.txt", 1)
version = Trim(f.ReadLine)
f.Close
shell.Run """" & root & "\versions\" & version & "\venv\Scripts\pythonw.exe""" -m localforge_ops.tray", 0, False
'@
$trayText = $trayText.Replace('%ROOT%', $RuntimeRoot)
Set-Content -LiteralPath $tray -Value $trayText -Encoding ASCII
$ops = Join-Path $bin 'localforge-ops.cmd'
$opsText = @'
@echo off
setlocal
set "ROOT=%~dp0.."
for /f "usebackq delims=" %%V in ("%ROOT%\current.txt") do set "VERSION=%%V"
if not defined VERSION exit /b 2
endlocal & "%ROOT%\versions\%VERSION%\venv\Scripts\localforge-ops.exe" %*
'@
Set-Content -LiteralPath $ops -Value $opsText -Encoding ASCII
$current = Join-Path $RuntimeRoot 'current.txt'
$temporary = "$current.tmp.$PID"
Set-Content -LiteralPath $temporary -Value $releaseVersion -Encoding ASCII
if (Test-Path -LiteralPath $current) { Move-Item -LiteralPath $temporary -Destination $current -Force } else { Move-Item -LiteralPath $temporary -Destination $current }
Write-Output (ConvertTo-Json @{status='ok'; version=$releaseVersion; runtime=$versionRoot; launcher=$launcher})
