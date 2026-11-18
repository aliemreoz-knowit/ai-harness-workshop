<#
setup.ps1 -- the one setup command of the tinystream harness workshop (Windows).

    powershell -ExecutionPolicy Bypass -File setup.ps1 [-Check] [-NoDocker] [-NoConsole]

It works in the folder it sits in: a git clone, or an unzipped "Download ZIP". Running it again finishes what a
stopped run left undone and changes nothing that works. It needs no admin rights and changes no system setting.
Everything it makes stays in this folder (.venv, .tools, .console-data), with these exceptions: when the Copilot CLI
is missing, winget installs it for your user only (and PowerShell 7, which its package needs, when that is
missing too); when no Python 3.12 is there, uv keeps the one it fetches in
%APPDATA%\uv and its cache in %LOCALAPPDATA%\uv\cache (the 260-character path limit of Windows rules out this folder
for them); the download cache of pip and the compiler cache of zig (%LOCALAPPDATA%\zig) stay in your user profile.

Network: PyPI (two pinned packages), GitHub (uv, only when no Python 3.12 is there, checked by SHA-256), winget
(the Copilot CLI, only when missing), npm (the editor, checked by the console's own script), and the Docker image
build. It sends no data anywhere.

The last line it prints is always "Depth: <depth>": Look, Try (fake model), Try (your Copilot),
Full (fake model) or Full (your Copilot). The exit code is 1 for Look, otherwise 0.
Windows PowerShell 5.1: no PowerShell 7 syntax in here, and ASCII only (5.1 reads this file in the ANSI code page).
#>
param(
    [switch]$Check,
    [switch]$NoDocker,
    [switch]$NoConsole,
    [switch]$Help,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

# --- pins ---------------------------------------------------------------------------------------------------
$SdkPin = 'github-copilot-sdk==1.0.23'
$ZigPin = 'ziglang==0.16.0'
$CopilotVersion = '1.0.100'
$UvVersion = '0.13.5'
$UvSha256 = @{  # the SHA-256 of a uv 0.13.5 archive, from the release's sha256.sum
    'uv-x86_64-pc-windows-msvc.zip'  = 'a38b8d1902b7cfbc76b423ba1bb456f623f42064cdc72628b6075b89afd1019a'
    'uv-aarch64-pc-windows-msvc.zip' = '09f47edf90726084a70f9426f6e3ca4f46e9bf7cbf23b1716ec64d60b055fae3'
}
$Image = 'tinystream-dev:latest'
$DOCTOR_TIMEOUT = 180
$WINGET_TIMEOUT = 600
$CONSOLE_WAIT = 60
$MAX_ROOT = 135  # 259 characters per file path, minus the 121 the deepest file in .venv needs, minus 3 spare
$LimitedOut = ''

function Show-Usage {
    Write-Host 'Usage: powershell -ExecutionPolicy Bypass -File setup.ps1 [-Check] [-NoDocker] [-NoConsole]'
    Write-Host ''
    Write-Host '  -Check       only look: report what is there; install, download and start nothing'
    Write-Host '  -NoDocker    do not build the tinystream-dev Docker image now'
    Write-Host '  -NoConsole   do not start the Harness Console'
    Write-Host ''
    Write-Host 'The last line is your depth, for example "Depth: Try (fake model)".'
}

# the Linux spellings work too (--check, --no-docker, --no-console, --help)
if ($Rest) {
    foreach ($arg in $Rest) {
        switch ($arg) {
            '--check' { $Check = [switch]$true }
            '--no-docker' { $NoDocker = [switch]$true }
            '--no-console' { $NoConsole = [switch]$true }
            '--help' { $Help = [switch]$true }
            '-h' { $Help = [switch]$true }
            default {
                Write-Host "setup.ps1: unknown option: $arg"
                Show-Usage
                exit 2
            }
        }
    }
}
if ($Help) {
    Show-Usage
    exit 0
}

$Root = $PSScriptRoot
Set-Location -LiteralPath $Root
$Start = Get-Date

function Say([string]$Text) { Write-Host $Text }
function Step([int]$N, [string]$Title) { Write-Host ''; Write-Host "[$N/8] $Title" }
function Ok([string]$Text) { Write-Host "  ok    $Text" }
function Did([string]$Text) { Write-Host "  done  $Text" }
function Note([string]$Text) { Write-Host "  note  $Text" }
function Fail([string]$Text) { Write-Host "  FAIL  $Text" }

function Finish([string]$Depth, [string[]]$Lines) {
    # the closing lines, the depth last
    Write-Host ''
    foreach ($line in @($Lines)) { if ($line) { Write-Host $line } }
    if ($Depth -eq 'Look') {
        Write-Host 'At Look depth you follow every exercise on the projector and in the repository on GitHub.'
    } elseif ($Depth -like '*(fake model)') {
        Write-Host 'Agent runs use the fake model here (no model, no credits).'
    }
    $secs = [int]((Get-Date) - $Start).TotalSeconds
    if ($Check) { Write-Host "Check finished in $secs s. Nothing was installed." }
    else { Write-Host "Setup finished in $secs s." }
    Write-Host "Depth: $Depth"
    if ($Depth -eq 'Look') { exit 1 }
    exit 0
}

function Join-Args([string[]]$List) {
    # one command line for Start-Process (Windows PowerShell 5.1 joins its -ArgumentList without quoting)
    $parts = foreach ($a in $List) {
        if ($a -eq '' -or $a -match '[\s"]') { '"' + (($a -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"' }
        else { $a }
    }
    return ($parts -join ' ')
}

function Read-Shared([string]$Path) {
    # the text of a file another process still writes (UTF-8), or ''
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
        try {
            $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
            return $sr.ReadToEnd()
        } finally { $fs.Close() }
    } catch { return '' }
}

function Invoke-Limited([string]$Exe, [string[]]$ArgList, [int]$Seconds, [string]$Dir) {
    # runs Exe with a time limit; sets $script:LimitedOut (stdout, then stderr, UTF-8) and returns the exit
    # code, or 124 when it ran out of time (then the whole process tree is stopped)
    $out = [System.IO.Path]::GetTempFileName()
    $err = [System.IO.Path]::GetTempFileName()
    $script:LimitedOut = ''
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList (Join-Args $ArgList) -WorkingDirectory $Dir -NoNewWindow `
            -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    } catch {
        $script:LimitedOut = $_.Exception.Message
        Remove-Item -LiteralPath $out, $err -ErrorAction SilentlyContinue
        return 127
    }
    $null = $p.Handle  # keeps ExitCode readable after the process ends
    if ($p.WaitForExit($Seconds * 1000)) {
        $p.WaitForExit()
        $code = $p.ExitCode
    } else {
        $null = & taskkill.exe /T /F /PID $p.Id 2>$null
        $code = 124
    }
    $script:LimitedOut = (@((Read-Shared $out).TrimEnd(), (Read-Shared $err).TrimEnd()) | Where-Object { $_ }) -join "`n"
    Remove-Item -LiteralPath $out, $err -ErrorAction SilentlyContinue
    return $code
}

function Invoke-Shown([string]$Exe, [string[]]$ArgList, [int]$Seconds) {
    # runs Exe in this window, so its output shows as it comes, with a time limit; returns the exit code, or 124
    # when it ran out of time (then the whole process tree is stopped)
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList (Join-Args $ArgList) -WorkingDirectory $Root -NoNewWindow -PassThru
    } catch {
        Write-Host "  $($_.Exception.Message)"
        return 127
    }
    $null = $p.Handle  # keeps ExitCode readable after the process ends
    if ($p.WaitForExit($Seconds * 1000)) {
        $p.WaitForExit()
        return $p.ExitCode
    }
    $null = & taskkill.exe /T /F /PID $p.Id 2>$null
    return 124
}

function Get-Download([string]$Url, [string]$OutFile) {
    # https only (no redirect to http), the system proxy with your Windows login
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor `
            [System.Net.SecurityProtocolType]::Tls12
        $wc = New-Object System.Net.WebClient
        if ($wc.Proxy) { $wc.Proxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials }
        $wc.DownloadFile($Url, $OutFile)
        return $true
    } catch {
        Write-Host "  $($_.Exception.Message)"
        return $false
    }
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

if ($Check) { Say 'tinystream harness workshop: setup check (nothing is installed)' }
else { Say 'tinystream harness workshop: setup' }
Say "Folder: $Root"

# Windows allows 260 characters for a file path unless long paths are switched on. The deepest file the setup
# writes is one of the zig C compiler's in .venv, 120 characters below this folder
# (.venv\Lib\site-packages\ziglang\lib\libc\mingw\lib-common\api-ms-win-core-processtopology-obsolete-l1-1-0_windowsapp.def):
# with a longer folder path pip fails halfway with a puzzling error, so the setup stops here instead.
$longPaths = $null
try {
    $longPaths = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled `
        -ErrorAction Stop).LongPathsEnabled
} catch { $longPaths = $null }
if ($Root.Length -gt $MAX_ROOT -and $longPaths -ne 1) {
    Fail "this folder's path has $($Root.Length) characters: too long for Windows, which allows 260 per file."
    Finish 'Look' @("The setup needs a folder path of at most $MAX_ROOT characters. Move the folder somewhere shorter, for example C:\Users\$env:USERNAME\ws, and run the setup again.")
}

# --- 1. git ------------------------------------------------------------------------------------------------
Step 1 'git'
$GitExe = $null
$gitCmd = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$gitVersion = ''
if ($gitCmd) {
    $GitExe = $gitCmd.Source
    $gitVersion = (@(& $GitExe --version 2>$null) -join ' ').Trim()
    if ($LASTEXITCODE -ne 0) { $gitVersion = '' }
}
if (-not $gitVersion) {
    Fail 'git is missing. Get Git for Windows from https://git-scm.com/downloads (it installs for your user too), then run the setup again.'
    Finish 'Look' @('The harness needs git: it commits your work and makes worktrees.')
}
Ok $gitVersion

function Git-Out {
    $o = & $script:GitExe @args 2>$null
    if ($LASTEXITCODE -ne 0) { return '' }
    return ((@($o) -join "`n").Trim())
}
function Git-Ok {
    $null = & $script:GitExe @args 2>$null
    return ($LASTEXITCODE -eq 0)
}

$dotGit = Join-Path $Root '.git'
$haveCommit = $false
if ((Test-Path -LiteralPath $dotGit) -and (Git-Ok rev-parse -q --verify HEAD)) {
    $haveCommit = $true
    Ok "this folder is a git repository (branch $(Git-Out rev-parse --abbrev-ref HEAD))"
} elseif ($Check) {
    Note 'this folder is not a git repository yet: the setup makes it one, with one commit "workshop start"'
} elseif (-not (Test-Path -LiteralPath $dotGit)) {
    if (-not (Git-Ok init -q -b main)) {  # git older than 2.28 has no -b
        $null = Git-Ok init -q
        $null = Git-Ok symbolic-ref HEAD refs/heads/main
    }
}
if ((Test-Path -LiteralPath $dotGit) -and (-not (Git-Out config user.name) -or -not (Git-Out config user.email))) {
    if ($Check) {
        Note 'git has no identity here: the setup sets Workshop participant <participant@localhost> for this folder'
    } else {
        if (-not (Git-Out config user.name)) { $null = Git-Ok config user.name 'Workshop participant' }
        if (-not (Git-Out config user.email)) { $null = Git-Ok config user.email 'participant@localhost' }
        Did 'set a git identity for this folder only (git config user.name / user.email)'
    }
}
if (-not $haveCommit -and -not $Check) {
    # the start commit stays on this laptop: no signing prompt, no company commit hook, no CRLF warnings
    & $GitExe -c core.safecrlf=false add -A
    $added = ($LASTEXITCODE -eq 0)
    if ($added) { & $GitExe -c commit.gpgsign=false commit -q --no-verify -m 'workshop start' }
    if ($added -and $LASTEXITCODE -eq 0) {
        Did 'made this folder a git repository: one commit "workshop start" on main'
    } else {
        Fail 'git could not make the first commit (see the message above).'
        Finish 'Look' @('Fix the git problem above and run the setup again.')
    }
}

# the workshop's branches (ticket/TS-1xx: each ticket's start state for an experiment; after-section-1 .. after-section-5: the state
# after each section): a clone from GitHub has them as origin/..., the experiments and the release run need them local
if ($GitExe -and (Test-Path .git) -and -not $Check) {
    $null = & $GitExe remote get-url origin 2>$null
    if ($LASTEXITCODE -eq 0) {
        $null = & $GitExe fetch -q origin '+refs/heads/ticket/*:refs/remotes/origin/ticket/*' '+refs/heads/after-section-*:refs/remotes/origin/after-section-*' 2>$null
        $made = 0
        foreach ($r in @(& $GitExe for-each-ref --format='%(refname:strip=3)' refs/remotes/origin/ 2>$null)) {
            if ($r -match '^(ticket/TS-[0-9]+|after-section-[1-5])$') {
                $null = & $GitExe branch -f $r "origin/$r" 2>$null
                if ($LASTEXITCODE -eq 0) { $made++ }
            }
        }
        if ($made -gt 0) { Did "the workshop's branches: $made local (ticket/TS-1xx, after-section-1 .. after-section-5)" }
    }
}

# --- 2. Python 3.12+ and .venv --------------------------------------------------------------------------------
Step 2 'Python 3.12 or newer'
$Vpy = $null
function Test-Venv {
    # sets $script:Vpy: .venv runs Python 3.12 or newer, with pip
    $p = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $p)) { return $false }
    $null = & $p -c 'import sys, pip; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
    if ($LASTEXITCODE -ne 0) { return $false }
    $script:Vpy = $p
    return $true
}

$PyExe = $null
$PyPre = @()
$PyVersion = ''
$PySeen = @()
function Find-Python {
    # sets $script:PyExe (and $script:PyPre for the py launcher): the first Python 3.12+ with working venv
    # and ensurepip modules. Every match on PATH counts: the Microsoft Store stub (python.exe in WindowsApps)
    # often comes first, fails here and is skipped
    $tried = @{}
    foreach ($cand in @(@('python'), @('python3'), @('py', '-3'))) {
        foreach ($cmd in @(Get-Command $cand[0] -CommandType Application -All -ErrorAction SilentlyContinue)) {
            $exe = $cmd.Source
            if ($tried.ContainsKey($exe)) { continue }
            $tried[$exe] = $true
            $pre = @($cand | Select-Object -Skip 1)
            $v = (@(& $exe @pre -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null) -join '').Trim()
            if ($LASTEXITCODE -ne 0 -or -not $v) { continue }
            $null = & $exe @pre -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 12) else 3)' 2>$null
            $rc = $LASTEXITCODE
            if ($rc -eq 0) {
                $script:PyExe = $exe
                $script:PyPre = $pre
                $script:PyVersion = $v
                return $true
            } elseif ($rc -eq 3) {
                $script:PySeen += "$exe $v (older than 3.12)"
            } else {
                $script:PySeen += "$exe $v (no venv module)"
            }
        }
    }
    return $false
}

$UvExe = Join-Path $Root '.tools\uv\uv.exe'
function Get-Uv {
    # .tools\uv\uv.exe: downloaded from the uv release on GitHub when missing, run only if its SHA-256 matches
    if (Test-Path -LiteralPath $UvExe) {
        $v = (@(& $UvExe --version 2>$null) -join ' ')
        if ($LASTEXITCODE -eq 0 -and $v -like "uv $UvVersion*") { Ok "uv $UvVersion in .tools\uv"; return $true }
    }
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
    if ($arch -eq 'AMD64') { $asset = 'uv-x86_64-pc-windows-msvc.zip' }
    elseif ($arch -eq 'ARM64') { $asset = 'uv-aarch64-pc-windows-msvc.zip' }
    else {
        Fail "there is no uv build for $arch Windows: install Python 3.12 or newer, then run the setup again."
        return $false
    }
    $want = $UvSha256[$asset]
    $url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/$asset"
    $dir = Join-Path $Root '.tools\uv'
    $part = Join-Path $dir "$asset.part"
    $stage = Join-Path $dir 'stage'
    $null = New-Item -ItemType Directory -Force -Path $dir
    Say "  downloading uv $UvVersion from GitHub (about 16 MB)"
    if (-not (Get-Download $url $part)) {
        Remove-Item -LiteralPath $part -ErrorAction SilentlyContinue
        Fail "could not download $url (network or proxy?)."
        return $false
    }
    $got = Get-Sha256 $part
    if ($got -ne $want) {
        Remove-Item -LiteralPath $part -ErrorAction SilentlyContinue
        Fail "the uv download has the wrong SHA-256 (got $got, want $want): not running it."
        return $false
    }
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::ExtractToDirectory($part, $stage)
        $found = Get-ChildItem -LiteralPath $stage -Recurse -Filter 'uv.exe' | Select-Object -First 1
        if (-not $found) { throw 'no uv.exe in the download' }
        Move-Item -LiteralPath $found.FullName -Destination $UvExe -Force
    } catch {
        Fail "could not unpack the uv download: $($_.Exception.Message)"
        Remove-Item -LiteralPath $stage, $part -Recurse -Force -ErrorAction SilentlyContinue
        return $false
    }
    Remove-Item -LiteralPath $stage, $part -Recurse -Force -ErrorAction SilentlyContinue
    Did "uv $UvVersion in .tools\uv (SHA-256 checked)"
    return $true
}

$venvDir = Join-Path $Root '.venv'
if (Test-Venv) {
    $venvVersion = (@(& $Vpy -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null) -join '').Trim()
    Ok ".venv works (Python $venvVersion)"
} else {
    $Vpy = $null
    if (Test-Path -LiteralPath $venvDir) {
        if ($Check) {
            Note '.venv is there but does not work (or is older than Python 3.12): the setup makes a new one'
        } else {
            Note '.venv is there but does not work (or is older than Python 3.12): making a new one'
            Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    $useUv = $false
    if (Find-Python) {
        Ok "Python $PyVersion ($((@($PyExe) + $PyPre) -join ' ')) has venv"
        if ($Check) {
            Note 'the setup makes .venv with it'
        } else {
            & $PyExe @PyPre -m venv $venvDir
            if ($LASTEXITCODE -eq 0) {
                Did 'made .venv'
            } else {
                Note 'python -m venv did not work: using uv instead'
                Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction SilentlyContinue
                $useUv = $true
            }
        }
    } else {
        $seen = ''
        if ($PySeen.Count -gt 0) { $seen = " (found: $($PySeen -join ', '))" }
        Note "no Python 3.12 or newer with a venv module on PATH$seen"
        $useUv = $true
    }
    if ($useUv) {
        if ($Check) {
            Note "the setup downloads uv $UvVersion (about 16 MB, SHA-256 checked) and lets it fetch Python 3.12"
        } elseif (Get-Uv) {
            # uv keeps the Python it fetches in its own place in your user profile (%APPDATA%\uv): inside this
            # folder its deep paths would hit the 260-character limit of Windows
            $env:UV_NO_CONFIG = '1'
            & $UvExe venv --seed --python 3.12 $venvDir
            $uvCode = $LASTEXITCODE
            Remove-Item Env:UV_NO_CONFIG -ErrorAction SilentlyContinue
            if ($uvCode -eq 0) { Did 'made .venv with uv (Python 3.12)' }
            else {
                Fail 'uv could not make .venv (see above).'
                Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
    if (-not $Check -and -not (Test-Venv)) {
        Fail 'there is no working Python 3.12 here.'
        Finish 'Look' @('The setup needs Python 3.12 or newer: install it (python.org, "for me only"), then run the setup again.')
    }
}
# the Python the harness runs with (in check mode, maybe the one on PATH)
$Pyx = $Vpy
$PyxPre = @()
if (-not $Pyx -and $PyExe) { $Pyx = $PyExe; $PyxPre = $PyPre }

# --- 3. packages ------------------------------------------------------------------------------------------------
Step 3 'Python packages'
$mark = Join-Path $Root '.venv\.workshop-packages'
$wantPkgs = "$SdkPin $ZigPin"
$pkgFailed = $false
$markText = ''
if ($Vpy -and (Test-Path -LiteralPath $mark)) { $markText = ([System.IO.File]::ReadAllText($mark)).Trim() }
if ($Vpy -and $markText -eq $wantPkgs) {
    Ok "$SdkPin and $ZigPin are installed"
} elseif ($Check) {
    Note "not installed yet: the setup installs $SdkPin and $ZigPin into .venv (about 110 MB)"
} else {
    Say "  installing $SdkPin and $ZigPin (about 110 MB; most of it is the zig C compiler)"
    Remove-Item -LiteralPath $mark -ErrorAction SilentlyContinue
    # the mark is written only after pip succeeded: a run stopped here installs everything again next time
    & $Vpy -m pip install --disable-pip-version-check --retries 1 --timeout 30 --force-reinstall $SdkPin $ZigPin
    if ($LASTEXITCODE -eq 0) {
        [System.IO.File]::WriteAllText($mark, "$wantPkgs`n")
        Did 'packages installed'
    } else {
        Fail 'pip could not install the packages (see its message above: network, proxy or a path too long). Run the setup again later: it retries.'
        $pkgFailed = $true
    }
}

# --- 4. Copilot CLI ------------------------------------------------------------------------------------------------
Step 4 'Copilot CLI'
$Cli = $null
function Find-Cli {
    # sets $script:Cli: the Copilot CLI the harness will use
    $script:Cli = $null
    if ($env:COPILOT_CLI_PATH -and (Test-Path -LiteralPath $env:COPILOT_CLI_PATH -PathType Leaf)) {
        $script:Cli = $env:COPILOT_CLI_PATH
        return $true
    }
    if ($Pyx -and (Test-Path -LiteralPath (Join-Path $Root 'harness'))) {
        # what the harness finds itself (an npm install); JSON keeps a non-ASCII path intact through the pipe
        $o = & $Pyx @PyxPre -c 'import json; from harness.envdetect import find_copilot_cli as f; print(json.dumps(dict(cli=f())))' 2>$null
        if ($LASTEXITCODE -eq 0 -and $o) {
            try { $found = (@($o)[-1] | ConvertFrom-Json).cli } catch { $found = $null }
            if ($found) { $script:Cli = $found; return $true }
        }
    }
    # where winget puts it (the harness does not look there): the doctor and the console get COPILOT_CLI_PATH
    if ($env:LOCALAPPDATA) {
        $cands = @(Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\copilot.exe')
        $pkgs = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
        if (Test-Path -LiteralPath $pkgs) {
            $cands += @(Get-ChildItem -LiteralPath $pkgs -Directory -Filter 'GitHub.Copilot_*' -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path $_.FullName 'copilot.exe' })
        }
        foreach ($c in $cands) {
            if (Test-Path -LiteralPath $c -PathType Leaf) {
                $script:Cli = $c
                $env:COPILOT_CLI_PATH = $c
                return $true
            }
        }
    }
    return $false
}
function Install-Cli {
    # returns only $true/$false: winget's own output goes straight to this window (progress, and its reason when
    # it fails); its agreement flags keep it from ever waiting for an answer, and it has a time limit
    $winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $winget) { Say '  winget is not available on this computer'; return $false }
    # winget names some versions with a "v" (v1.0.100): take the name it offers
    $ver = $null
    $null = Invoke-Limited $winget.Source @('show', '--id', 'GitHub.Copilot', '-e', '--source', 'winget', '--versions',
        '--accept-source-agreements', '--disable-interactivity') 60 $Root
    foreach ($line in ($LimitedOut -split "`r?`n")) {
        $t = "$line".Trim()
        if ($t -match ('^v?' + [regex]::Escape($CopilotVersion) + '$')) { $ver = $t; break }
    }
    $argList = @('install', '--id', 'GitHub.Copilot', '-e', '--source', 'winget', '--scope', 'user')
    if ($ver) {
        Say "  installing Copilot CLI $CopilotVersion with winget, for your user only (up to $WINGET_TIMEOUT s)"
        $argList += @('--version', $ver)
    } else {
        Say "  winget does not offer ${CopilotVersion}: installing its newest Copilot CLI, for your user only (the doctor warns about the version)"
    }
    $argList += @('--accept-source-agreements', '--accept-package-agreements', '--disable-interactivity')
    $code = Invoke-Shown $winget.Source $argList $WINGET_TIMEOUT
    if ($code -eq 124) { Say "  winget did not finish within $WINGET_TIMEOUT s: stopped" }
    elseif ($code -ne 0) { Say "  winget ended with exit code $code" }
    return ($code -eq 0)
}

# The harness needs exactly Copilot CLI $CopilotVersion: a newer CLI speaks another protocol than the pinned SDK. So the
# setup installs that version into .tools\copilot with npm, whatever CLI you have already, and the harness looks there
# first and starts it with auto-update off. Your own CLI stays as it is.
$LocalCliDir = Join-Path $Root '.tools\copilot'
function Get-LocalCli {
    $c = Get-ChildItem -LiteralPath (Join-Path $LocalCliDir 'node_modules\@github') -Directory -Filter 'copilot-*' `
        -ErrorAction SilentlyContinue | ForEach-Object { Join-Path $_.FullName 'copilot.exe' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    return $c
}
# The CLI's own package for this computer, straight from the npm registry (no Node.js needed), checked by its sha512.
$CliPackages = @{
    'x64'   = 'npcsVKICWbflvURVqkKD2CXx8ofbL3Odj/JUXi4vPbf/u75+oS2YLtWbYnx9l3Xc0Ejm3maFs7r5aww67ikgyw=='
    'arm64' = '0+1nORk1kcvVMbuEYoFAe7DtV3ZdAaY7XguxqqZwd4R7V1yOPQyVvU3H65t3DKjZ41SIcT+to4sRD8R0JOjvDw=='
}
function Install-LocalCli {
    $npm = Get-Command npm -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($npm) {
        Say "  installing Copilot CLI $CopilotVersion into .tools\copilot with npm (about 130 MB)"
        & $npm.Source install --prefix $LocalCliDir --no-fund --no-audit "@github/copilot@$CopilotVersion" | Out-Host
        if (Get-LocalCli) { return $true }
    }
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
    $pkg = "copilot-win32-$arch"
    $url = "https://registry.npmjs.org/@github/$pkg/-/$pkg-$CopilotVersion.tgz"
    $tgz = Join-Path $Root ".tools\$pkg-$CopilotVersion.tgz"
    $dest = Join-Path $LocalCliDir "node_modules\@github\$pkg"
    Say "  downloading Copilot CLI $CopilotVersion ($pkg, about 60 MB) into .tools\copilot"
    try {
        New-Item -ItemType Directory -Force (Split-Path $tgz) | Out-Null
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tgz -TimeoutSec 600
        $sha = [System.Security.Cryptography.SHA512]::Create()
        $stream = [System.IO.File]::OpenRead($tgz)
        try { $got = [Convert]::ToBase64String($sha.ComputeHash($stream)) } finally { $stream.Dispose() }
        if ($got -ne $CliPackages[$arch]) { Say '  the download has the wrong sha512: not using it'; Remove-Item -Force $tgz; return $false }
        New-Item -ItemType Directory -Force $dest | Out-Null
        & tar.exe -xzf $tgz -C $dest --strip-components=1
        Remove-Item -Force $tgz
    } catch {
        Say "  could not download it: $($_.Exception.Message)"
        return $false
    }
    return [bool](Get-LocalCli)
}

if (Get-LocalCli) {
    $null = Find-Cli
    Ok "Copilot CLI $CopilotVersion in .tools\copilot: $Cli"
} elseif ($Check) {
    Note "not here yet: the setup installs Copilot CLI $CopilotVersion into .tools\copilot"
} elseif ((Install-LocalCli) -and (Find-Cli)) {
    Did "Copilot CLI $CopilotVersion in .tools\copilot: $Cli"
} elseif (Find-Cli) {
    Note "Copilot CLI $CopilotVersion could not be installed here; the harness uses $Cli, which may be too new for it"
} elseif ((Install-Cli) -and (Find-Cli)) {
    Did "Copilot CLI: $Cli (not ${CopilotVersion}: the agent runs may fail, run the setup again when the network works)"
} else {
    $Cli = $null
    Fail 'the Copilot CLI is not installed, so you stay on the fake model. Later: https://gh.io/install-copilot-readme, then run the setup again.'
}

# --- 5. editor -------------------------------------------------------------------------------------------------------
Step 5 'code editor for the console'
$monaco = 'console\scripts\vendor_monaco.py'
if (-not (Test-Path -LiteralPath (Join-Path $Root $monaco))) {
    Note 'no console folder here: skipped'
} elseif (-not $Pyx) {
    Note 'needs Python 3.12: skipped'
} else {
    $null = & $Pyx @PyxPre $monaco --check 2>$null
    if ($LASTEXITCODE -eq 0) {
        Ok 'the Monaco editor is installed'
    } elseif ($Check) {
        Note 'not installed: the setup downloads the Monaco editor (about 5 MB, checked)'
    } else {
        & $Pyx @PyxPre $monaco
        if ($LASTEXITCODE -eq 0) { Did 'the Monaco editor is installed' }
        else { Note 'the editor could not be downloaded: the console uses a plain text box. Run the setup again later.' }
    }
}

# --- 6. Docker -----------------------------------------------------------------------------------------------------
Step 6 'Docker (for the Full depth)'
$dockerUp = $false
$imageReady = $false
function Find-GitBash {
    # Git Bash (never C:\Windows\System32\bash.exe, which is WSL)
    $roots = @()
    $d = Split-Path -Parent $GitExe
    for ($i = 0; $i -lt 3 -and $d; $i++) { $roots += $d; $d = Split-Path -Parent $d }
    if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles 'Git') }
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Programs\Git') }
    foreach ($r in $roots) {
        foreach ($rel in @('bin\bash.exe', 'usr\bin\bash.exe')) {
            $c = Join-Path $r $rel
            if (Test-Path -LiteralPath $c -PathType Leaf) { return $c }
        }
    }
    return $null
}
$dockerCmd = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $dockerCmd -and $env:ProgramFiles) {
    # a window opened before Docker Desktop was installed has the old PATH: look where Docker Desktop puts it
    $dockerBin = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin'
    if (Test-Path -LiteralPath (Join-Path $dockerBin 'docker.exe') -PathType Leaf) {
        $env:Path = "$env:Path;$dockerBin"
        Note "Docker is not on this window's PATH, used anyway: $dockerBin (a new window has it)"
        $dockerCmd = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    }
}
if (-not $dockerCmd) {
    Note 'Docker is not installed: the gates run without it (Try depth).'
} elseif ((Invoke-Limited $dockerCmd.Source @('info') 8 $Root) -ne 0) {
    Note 'Docker is installed but does not answer (not running?): the gates run without it. Start Docker Desktop and run the setup again for the Full depth.'
} else {
    $dockerUp = $true
    $null = & $dockerCmd.Source image inspect $Image 2>$null
    if ($LASTEXITCODE -eq 0) { $imageReady = $true }
    # Full depends on the image only: once it is there, a rerun builds nothing and cannot lower the depth
    if ($imageReady) {
        Ok "Docker runs and the $Image image is there"
    } elseif ($NoDocker) {
        Note "the $Image image is not built: skipped (-NoDocker)"
    } elseif ($Check) {
        Note "Docker runs: the setup builds the $Image image (about 1 GB to download)"
    } else {
        $bash = Find-GitBash
        if (-not $bash) {
            Note 'the Docker build needs Git Bash (part of Git for Windows): skipped'
        } else {
            Say "  Docker runs. Building the $Image image: about 1 GB to download and 3 GB on disk."
            Say '  This takes several minutes (often 10 to 15), once. Later runs reuse the image.'
            & $bash -c 'bash scripts/build.sh host </dev/null'
            $buildCode = $LASTEXITCODE
            $null = & $dockerCmd.Source image inspect $Image 2>$null
            if ($LASTEXITCODE -eq 0) {
                $imageReady = $true
                if ($buildCode -eq 0) { Did "the $Image image is built" }
                else { Note "the $Image image is built; compiling the code in it failed (see above). The gates will show it." }
            } else {
                Fail 'the Docker build failed (see above). You are at Try depth until it works: run the setup again to retry.'
            }
        }
    }
}

# The ladder the harness and the console use from here on, recorded in .tools\ladder (rewritten on every run, so a rerun
# without -NoDocker after installing Docker says "full"): "full" only when the image is there and Docker was not turned
# off with -NoDocker; "lite" (zig, no Docker) otherwise. The console reads it once, at its start.
$ladder = 'lite'
if ($imageReady -and -not $NoDocker) { $ladder = 'full' }
if (-not $Check) {
    try {
        $null = New-Item -ItemType Directory -Force -Path (Join-Path $Root '.tools')
        [System.IO.File]::WriteAllText((Join-Path $Root '.tools\ladder'), "$ladder`n", (New-Object System.Text.UTF8Encoding($false)))
    } catch { Note 'could not write .tools\ladder' }
}

# --- 7. doctor --------------------------------------------------------------------------------------------------------
Step 7 'doctor'
if (-not (Test-Path -LiteralPath (Join-Path $Root 'harness'))) {
    Fail 'there is no harness folder: is this the workshop repository?'
    Finish 'Look' @('Download the workshop repository again and run the setup in its folder.')
}
if (-not $Pyx) {
    Fail 'the doctor needs Python 3.12 or newer'
    Finish 'Look' @('Run the setup without -Check: it gets Python 3.12.')
}
$hideDocker = $false
if ($dockerUp -and $ladder -eq 'lite') {
    # the doctor would start the image build itself (or undo -NoDocker): it checks the Docker-free ladder instead
    if ($NoDocker) { Note '-NoDocker: the doctor checks the gates without Docker' }
    else { Note "Docker runs but the $Image image is not built: the doctor checks the gates without Docker" }
    $hideDocker = $true
}
$Depth = ''
function Invoke-Doctor([int]$Seconds, [string[]]$Extra, [bool]$Hide) {
    # sets $script:Depth from the doctor's "Depth:" line; prints the doctor's other lines
    $oldHost = $env:DOCKER_HOST
    $env:PYTHONIOENCODING = 'utf-8'
    if ($Hide) { $env:DOCKER_HOST = 'npipe:////./pipe/tinystream_no_docker' }
    $argList = @($PyxPre) + @('-m', 'harness', 'doctor', '--depth') + @($Extra | Where-Object { $_ })
    $code = Invoke-Limited $Pyx $argList $Seconds $Root
    Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
    if ($Hide) {
        if ($null -eq $oldHost) { Remove-Item Env:DOCKER_HOST -ErrorAction SilentlyContinue } else { $env:DOCKER_HOST = $oldHost }
    }
    $script:Depth = ''
    foreach ($line in ($LimitedOut -split "`r?`n")) {
        if ($line -match '^Depth: (.*)$') { $script:Depth = $Matches[1].Trim() }
        elseif ($line -notmatch '^Runtime mode:') { Write-Host $line }  # it names the facilitator's runner
    }
    if ($script:Depth -notmatch '^(Try|Full) \((fake model|your Copilot)\)$') {
        $script:Depth = ''
        if ($code -eq 124) { Say "  the doctor did not finish within $Seconds s" }
        else { Say "  the doctor gave no depth (exit code $code)" }
    }
}
if ($Cli) {
    Say '  checking your Copilot login: this can take up to 5 minutes; please wait, do not close the window'
    Invoke-Doctor $DOCTOR_TIMEOUT @() $hideDocker
} else {
    Invoke-Doctor $DOCTOR_TIMEOUT @('--skip-auth') $hideDocker
}
# no answer: once more without what can hang (the login check, the Docker probes), so it ends at Try, not at Look
$retryWithout = @()
if ($Cli) { $retryWithout += 'the login check' }
if ($dockerUp -and -not $hideDocker) { $retryWithout += 'Docker' }
if (-not $Depth -and $retryWithout.Count -gt 0) {
    Say "  once more, without $($retryWithout -join ' and '):"
    $hideDocker = $true
    Invoke-Doctor 120 @('--skip-auth') $true
}
if (-not $Depth) {
    Finish 'Look' @('The doctor could not run, so the setup cannot tell what works here. Fix what the lines above say, or ask for help.')
}
# Full needs the image, and a doctor that saw Docker
if ((-not $imageReady -or $hideDocker) -and $Depth -like 'Full*') { $Depth = 'Try' + $Depth.Substring(4) }
$why = ''
if ($pkgFailed) { $why = 'Some Python packages are missing: run the setup again when the network works.' }
elseif ($Cli -and $Depth -like '*(fake model)') {
    $cliCmd = if ($Cli -like '*\.tools\copilot\*') { "& `"$Cli`" --no-auto-update" } else { 'copilot' }
    $why = "To use your own Copilot: run $cliCmd, type /login, then run the setup again."
}

# --- 8. console ---------------------------------------------------------------------------------------------------------
Step 8 'Harness Console'
if ($Check) {
    Note 'not started (-Check)'
} elseif ($NoConsole) {
    Note 'not started (-NoConsole)'
} elseif (-not (Test-Path -LiteralPath (Join-Path $Root 'console\console'))) {
    Note 'no console folder here: skipped'
} else {
    $tools = Join-Path $Root '.tools'
    $null = New-Item -ItemType Directory -Force -Path $tools
    $log = Join-Path $tools 'console.log'
    # a console that runs still writes the last log: then this start writes a new one
    try {
        if (Test-Path -LiteralPath $log) { Move-Item -LiteralPath $log -Destination "$log.old" -Force -ErrorAction Stop }
    } catch {
        $log = Join-Path $tools ('console-' + (Get-Date).ToString('yyyyMMdd-HHmmss') + '.log')
    }
    $logName = $log.Substring($Root.Length + 1)
    # through cmd.exe and the shell, in a hidden console of its own: it inherits no handle of this window, so it
    # keeps running after the window closes and holds no pipe of a caller open. A second start prints the address
    # of the console that runs.
    $comspec = $env:ComSpec
    if (-not $comspec) { $comspec = 'cmd.exe' }
    $cmdLine = '/d /c ""' + $Vpy + '" -m console serve --personal > "' + $log + '" 2>&1"'
    $env:PYTHONIOENCODING = 'utf-8'
    $cp = $null
    try {
        $cp = Start-Process -FilePath $comspec -ArgumentList $cmdLine -WorkingDirectory (Join-Path $Root 'console') `
            -WindowStyle Hidden -PassThru
    } catch {
        Say "  $($_.Exception.Message)"
    }
    Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
    $url = ''
    $text = ''
    $deadline = (Get-Date).AddSeconds($CONSOLE_WAIT)
    while ($cp -and (Get-Date) -lt $deadline) {
        $text = Read-Shared $log
        if ($text -match 'http://(127\.0\.0\.1|localhost):[0-9]+') { $url = $Matches[0]; break }
        if ($cp.HasExited) { break }
        Start-Sleep -Milliseconds 200
    }
    if (-not $url -and $cp) {
        $text = Read-Shared $log
        if ($text -match 'http://(127\.0\.0\.1|localhost):[0-9]+') { $url = $Matches[0] }
    }
    if ($url) {
        if ($text -match 'already running') { Ok "the console is already running: $url" }
        else { Ok "the console runs in the background: $url" }
        $stopPid = $null
        try {
            $rec = [System.IO.File]::ReadAllText((Join-Path $Root '.console-data\serve.json')) | ConvertFrom-Json
            $stopPid = $rec.pid
        } catch { $stopPid = $null }
        if ($stopPid) { Say "        open it in your browser; to stop it: Stop-Process -Id $stopPid (log: $logName)" }
        else { Say "        open it in your browser (log: $logName)" }
    } elseif ($cp -and -not $cp.HasExited) {
        Note "the console is still starting: its address will be in $logName"
    } else {
        $tail = @((Read-Shared $log) -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 8)
        foreach ($line in $tail) { Say "        $line" }
        Note "the console did not start (log: $logName). The harness works without it."
    }
}

Finish $Depth @($why)
