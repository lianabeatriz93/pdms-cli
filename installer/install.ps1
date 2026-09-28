# pdms installer for Windows.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://github.com/lianabeatriz93/pdms-cli/releases/latest/download/install.ps1 | iex"
#
# Options (environment variables):
#   $env:PDMS_VERSION = "0.2.0"     install that version instead of the latest release
#   $env:PDMS_WHEEL = "<path|url>"  install that package file instead of downloading a release (used by CI)

$ErrorActionPreference = "Stop"
$Repo = "lianabeatriz93/pdms-cli"

function Find-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

$uv = Find-Uv
if (-not $uv) {
    Write-Host "Installing uv (https://docs.astral.sh/uv/)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $uv = Find-Uv
    if (-not $uv) { throw "uv was installed but cannot be found; open a new terminal and run the installer again" }
}

if ($env:PDMS_WHEEL) {
    $source = $env:PDMS_WHEEL
} else {
    $version = $env:PDMS_VERSION
    if (-not $version) {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -UseBasicParsing
        $version = $release.tag_name
    }
    $version = $version.TrimStart("v")
    $source = "https://github.com/$Repo/releases/download/v$version/pdms_cli-$version-py3-none-any.whl"
}

Write-Host "Installing pdms from $source"
& $uv tool install --force $source
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed (exit code $LASTEXITCODE)" }
& $uv tool update-shell 2>$null | Out-Null

$binDir = (& $uv tool dir --bin).Trim()
& (Join-Path $binDir "pdms.exe") --version
if ($LASTEXITCODE -ne 0) { throw "pdms was installed but does not start" }

if (-not (Get-Command pdms -ErrorAction SilentlyContinue)) {
    Write-Host "Open a new terminal to use pdms (it is installed in $binDir)."
}
if (-not (Get-Command poetry -ErrorAction SilentlyContinue)) {
    Write-Host "Note: running PDMS services also needs Poetry: https://python-poetry.org/docs/#installation"
}
Write-Host "Done. Next: pdms --help  -  tab completion: pdms --install-completion  -  updates: pdms self-update"
