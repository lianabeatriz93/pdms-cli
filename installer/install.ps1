# pdms installer for Windows.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://github.com/lianabeatriz93/pdms-cli/releases/latest/download/install.ps1 | iex"
#
# Options (environment variables):
#   $env:PDMS_VERSION = "0.2.0"     install that version instead of the latest release
#   $env:PDMS_PRERELEASE = "1"      install the latest release including alpha/beta pre-releases
#   $env:PDMS_WHEEL = "<path|url>"  install that package file instead of downloading a release (used by CI)
#   $env:PDMS_MENU = "1" | "0"      add pdms to the Start menu (pdms ui --install) without asking, or do not

$ErrorActionPreference = "Stop"
$Repo = "lianabeatriz93/pdms-cli"

# Windows PowerShell 5.1 turns anything a native program writes to stderr (uv prints its progress there) into an
# error record, which "Stop" makes fatal. Run native commands with "Continue" and judge them by their exit code.
function Invoke-Native([scriptblock]$Command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $previous }
}

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
    Invoke-Native { powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" }
    $uv = Find-Uv
    if (-not $uv) { throw "uv was installed but cannot be found; open a new terminal and run the installer again" }
}

if ($env:PDMS_WHEEL) {
    $source = $env:PDMS_WHEEL
} else {
    $version = $env:PDMS_VERSION
    if (-not $version -and $env:PDMS_PRERELEASE) {
        # The API lists every release (pre-releases included), newest first.
        $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases?per_page=1" -UseBasicParsing
        $version = @($releases)[0].tag_name
    }
    if (-not $version) {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -UseBasicParsing
        $version = $release.tag_name
    }
    $version = $version.TrimStart("v")
    $source = "https://github.com/$Repo/releases/download/v$version/pdms_cli-$version-py3-none-any.whl"
}

Write-Host "Installing pdms from $source"
# Without --python uv may take an older Python it finds first.
Invoke-Native { & $uv tool install --force --python ">=3.10" $source }
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed (exit code $LASTEXITCODE)" }
Invoke-Native { & $uv tool update-shell | Out-Null }

$binDir = (Invoke-Native { & $uv tool dir --bin } | Select-Object -Last 1).ToString().Trim()
Invoke-Native { & (Join-Path $binDir "pdms.exe") --version }
if ($LASTEXITCODE -ne 0) { throw "pdms was installed but does not start" }

# The Start menu shortcut (pdms ui --install), asked only when someone can answer.
$menu = $env:PDMS_MENU
if (-not $menu -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
    $answer = Read-Host "Add pdms to the Start menu (pdms ui in a window of its own)? [Y/n]"
    $menu = if ($answer -match "^[nN]") { "0" } else { "1" }
}
if ($menu -eq "1") {
    Invoke-Native { & (Join-Path $binDir "pdms.exe") ui --install }
    if ($LASTEXITCODE -ne 0) { Write-Host "Could not add pdms to the Start menu; try later with: pdms ui --install" }
}

if (-not (Get-Command pdms -ErrorAction SilentlyContinue)) {
    Write-Host "Open a new terminal to use pdms (it is installed in $binDir)."
}
if (-not (Get-Command poetry -ErrorAction SilentlyContinue)) {
    Write-Host "Note: running PDMS services also needs Poetry: https://python-poetry.org/docs/#installation"
}
Write-Host "Done. Next: pdms --help  -  tab completion: pdms --install-completion  -  updates: pdms self-update"
