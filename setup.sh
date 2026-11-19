#!/usr/bin/env bash
# setup.sh -- the one setup command of the tinystream harness workshop (Linux, macOS; on Windows in Git Bash too).
#
#   bash setup.sh [--check] [--no-console]
#
# It works in the folder it sits in: a git clone, or an unzipped "Download ZIP". Running it again finishes what
# a stopped run left undone and changes nothing that works. It needs no admin rights and edits no shell profile.
# Everything it makes stays in this folder (.venv, .tools, .console-data), except the Copilot CLI (when that is
# missing, GitHub's install script puts it in ~/.local/bin; on Windows npm or winget installs it for your user) and the download cache of pip and the compiler cache of
# zig (~/.cache/zig), which stay in your home folder.
#
# Docker is never used: the gates run without it (the zig C compiler), so --no-docker is accepted and changes nothing.
#
# Network: PyPI (two pinned packages), GitHub (uv and the Copilot CLI, only when missing, checked by SHA-256; the
# workshop's branches, for a ZIP folder) and npm (the editor, checked by the console's own script). It sends no data
# anywhere.
#
# The last line it prints is always "Depth: <depth>": Look, Try (fake model) or Try (your Copilot). The exit code is
# 1 for Look, otherwise 0.

if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -u

# --- pins ---------------------------------------------------------------------------------------------------
SDK_PIN="github-copilot-sdk==1.0.23"
ZIG_PIN="ziglang==0.16.0"
COPILOT_VERSION="1.0.100"
# GitHub's install script at a fixed commit of github/copilot-cli, and its SHA-256
COPILOT_INSTALL_URL="https://raw.githubusercontent.com/github/copilot-cli/752496d8c1e5db3ff9e7ff2dc460edc3e86d38c2/install.sh"
COPILOT_INSTALL_SHA256="00efcc2e196f0793d097935d6230062fc66e8247dc01334d2ac3eae4e26a1c7f"
UV_VERSION="0.13.5"
IMAGE="tinystream-dev:latest"
DOCTOR_TIMEOUT=180
CONSOLE_WAIT=60

uv_sha256() {  # the SHA-256 of a uv 0.13.5 archive, from the release's sha256.sum
    case "$1" in
        uv-x86_64-unknown-linux-gnu.tar.gz) echo f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f ;;
        uv-aarch64-unknown-linux-gnu.tar.gz) echo 4bb69cf95ed13f23713bb7bc09f241896ea0c75b7c557e615a20604b82f9c403 ;;
        uv-x86_64-apple-darwin.tar.gz) echo b0f52b0e3945b4107d89024ea38e625683011fbfc05a0d98f9fa2d6dcee28c66 ;;
        uv-aarch64-apple-darwin.tar.gz) echo 743f85035a1996ecb350659e01cd9faea9709829be09b1b4b03c73eaf3c9cd73 ;;
        *) return 1 ;;
    esac
}

usage() {
    cat <<'EOF'
Usage: bash setup.sh [--check] [--no-console]

  --check       only look: report what is there; install, download and start nothing
  --no-console  do not start the Harness Console
  (--no-docker is accepted and changes nothing: Docker is never used)

The last line is your depth, for example "Depth: Try (fake model)".
EOF
}

CHECK=0
NO_DOCKER=0
NO_CONSOLE=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK=1 ;;
        --no-docker) NO_DOCKER=1 ;;
        --no-console) NO_CONSOLE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "setup.sh: unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "$0")" && pwd)" || exit 1
cd "$ROOT" || exit 1
OS="$(uname -s 2>/dev/null || echo unknown)"
ARCH="$(uname -m 2>/dev/null || echo unknown)"
START="$(date +%s)"

say() { printf '%s\n' "$*"; }
step() { printf '\n[%s/8] %s\n' "$1" "$2"; }
ok() { printf '  ok    %s\n' "$*"; }
did() { printf '  done  %s\n' "$*"; }
note() { printf '  note  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; }

finish() {  # finish DEPTH [LINE...]: the closing lines, the depth last
    local depth="$1" line
    shift
    say ""
    for line in "$@"; do
        [ -n "$line" ] && say "$line"
    done
    case "$depth" in
        Look) say "At Look depth you follow every exercise on the projector and in the repository on GitHub." ;;
        *"(fake model)") say "Agent runs use the fake model here (no model, no credits)." ;;
    esac
    if [ "$CHECK" = 1 ]; then
        say "Check finished in $(( $(date +%s) - START )) s. Nothing was installed."
    else
        say "Setup finished in $(( $(date +%s) - START )) s."
    fi
    say "Depth: $depth"
    if [ "$depth" = Look ]; then exit 1; fi
    exit 0
}

run_limited() {  # run_limited SECONDS OUTFILE COMMAND...: the command's status; 124 when it ran out of time
    local secs="$1" out="$2" pid ticks=0
    shift 2
    "$@" >"$out" 2>&1 </dev/null &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$ticks" -ge $((secs * 5)) ]; then
            kill "$pid" 2>/dev/null
            sleep 1
            kill -9 "$pid" 2>/dev/null
            wait "$pid" 2>/dev/null
            return 124
        fi
        sleep 0.2
        ticks=$((ticks + 1))
    done
    wait "$pid"
}

download() {  # download URL FILE: https only, gives up quickly when the network is blocked
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --proto '=https' --retry 2 --connect-timeout 20 --speed-limit 1024 --speed-time 60 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --https-only --timeout=30 --tries=2 -O "$2" "$1"
    else
        say "  neither curl nor wget is installed"
        return 1
    fi
}

sha256_of() {  # sha256_of FILE: the hex digest, or nothing
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum <"$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 <"$1" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 <"$1" | awk '{print $NF}'
    fi
}

if [ "$CHECK" = 1 ]; then
    say "tinystream harness workshop: setup check (nothing is installed)"
else
    say "tinystream harness workshop: setup"
fi
say "Folder: $ROOT"
case "$OS" in
    MINGW*|MSYS*|CYGWIN*)
        say "On Windows the setup command is: powershell -ExecutionPolicy Bypass -File setup.ps1 (Git Bash works too)"
        # a terminal opened before Docker Desktop, Node.js or the Copilot CLI was installed has the old PATH:
        # look where they install, so a fresh install counts without a new terminal
        WIN_ADDED=""
        for d in "/c/Program Files/Docker/Docker/resources/bin" "/c/Program Files/nodejs" \
                 "${APPDATA:+$(cygpath -u "$APPDATA" 2>/dev/null)/npm}" \
                 "${LOCALAPPDATA:+$(cygpath -u "$LOCALAPPDATA" 2>/dev/null)/Microsoft/WinGet/Links}"; do
            [ -n "$d" ] && [ -d "$d" ] || continue
            case ":$PATH:" in
                *":$d:"*) ;;
                *) PATH="$PATH:$d"; WIN_ADDED="${WIN_ADDED:+$WIN_ADDED, }$d" ;;
            esac
        done
        export PATH
        [ -z "$WIN_ADDED" ] || note "not on this terminal's PATH, used anyway: $WIN_ADDED (a new terminal has them)"
        ;;
esac

# --- 1. git ------------------------------------------------------------------------------------------------
step 1 "git"
if ! GIT_VERSION="$(git --version 2>/dev/null)"; then
    fail "git is missing. Get it from https://git-scm.com/downloads (macOS: xcode-select --install), then run the setup again."
    finish Look "The harness needs git: it commits your work and makes worktrees."
fi
ok "$GIT_VERSION"

HAVE_COMMIT=0
if [ -e .git ] && git rev-parse -q --verify HEAD >/dev/null 2>&1; then
    HAVE_COMMIT=1
    ok "this folder is a git repository (branch $(git rev-parse --abbrev-ref HEAD 2>/dev/null))"
elif [ "$CHECK" = 1 ]; then
    note "this folder is not a git repository yet: the setup makes it one, with one commit \"workshop start\""
elif [ ! -e .git ]; then
    if ! git init -q -b main 2>/dev/null; then  # git older than 2.28 has no -b
        git init -q && git symbolic-ref HEAD refs/heads/main
    fi
fi
if [ -e .git ] && { [ -z "$(git config user.name 2>/dev/null)" ] || [ -z "$(git config user.email 2>/dev/null)" ]; }; then
    if [ "$CHECK" = 1 ]; then
        note "git has no identity here: the setup sets Workshop participant <participant@localhost> for this folder"
    else
        [ -n "$(git config user.name 2>/dev/null)" ] || git config user.name "Workshop participant"
        [ -n "$(git config user.email 2>/dev/null)" ] || git config user.email "participant@localhost"
        did "set a git identity for this folder only: $(git config user.name) <$(git config user.email)>"
    fi
fi
if [ "$HAVE_COMMIT" = 0 ] && [ "$CHECK" = 0 ]; then
    # the start commit stays on this laptop: no signing prompt, no company commit hook, no CRLF warnings
    if git -c core.safecrlf=false add -A && git -c commit.gpgsign=false commit -q --no-verify -m "workshop start"; then
        did "made this folder a git repository: one commit \"workshop start\" on main"
    else
        fail "git could not make the first commit (see the message above)."
        finish Look "Fix the git problem above and run the setup again."
    fi
fi

# the workshop's branches (ticket/TS-1xx: each ticket's start state for an experiment; after-section-1 .. after-section-5: the state
# after each section): a clone from GitHub has them as origin/..., the experiments and the release run need them local
if [ -e .git ] && [ "$CHECK" = 0 ] && ! git remote get-url origin >/dev/null 2>&1; then
    # an unzipped "Download ZIP" has no origin: the public repository is, read-only (nothing is ever pushed)
    git remote add origin "https://github.com/aliemreoz-knowit/ai-harness-workshop.git" 2>/dev/null &&
        did "origin: https://github.com/aliemreoz-knowit/ai-harness-workshop.git (to fetch the workshop's branches)"
fi
if [ -e .git ] && [ -f workshop/branches.sh ] && git remote get-url origin >/dev/null 2>&1; then
    if out="$(bash workshop/branches.sh 2>&1)"; then did "$out"; else note "$out"; fi
fi

# --- 2. Python 3.12+ and .venv --------------------------------------------------------------------------------
step 2 "Python 3.12 or newer"
VPY=""
venv_python() {
    if [ -x "$ROOT/.venv/bin/python" ]; then printf '%s\n' "$ROOT/.venv/bin/python"; return 0; fi
    if [ -f "$ROOT/.venv/Scripts/python.exe" ]; then printf '%s\n' "$ROOT/.venv/Scripts/python.exe"; return 0; fi
    return 1
}
venv_works() {  # sets VPY: .venv runs Python 3.12 or newer, with pip
    local p
    p="$(venv_python)" || return 1
    "$p" -c 'import sys, pip; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1 || return 1
    VPY="$p"
}

PY=""
PY_VERSION=""
PY_SEEN=""
find_python() {  # sets PY: the first Python 3.12+ on PATH with working venv and ensurepip modules
    local c p v rc
    for c in python3 python python3.14 python3.13 python3.12; do
        p="$(command -v "$c" 2>/dev/null)" || continue
        [ -n "$p" ] || continue
        # macOS: /usr/bin/python3 is Apple's 3.9, and without the developer tools it opens an install dialog
        if [ "$OS" = Darwin ] && [ "$p" = /usr/bin/python3 ]; then continue; fi
        v="$("$p" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)" || continue
        "$p" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 12) else 3)' >/dev/null 2>&1
        rc=$?
        if [ "$rc" = 0 ]; then
            PY="$p"
            PY_VERSION="$v"
            return 0
        elif [ "$rc" = 3 ]; then
            PY_SEEN="$PY_SEEN, $c $v (older than 3.12)"
        else
            PY_SEEN="$PY_SEEN, $c $v (no venv module)"
        fi
    done
    return 1
}

uv_asset() {
    case "$OS/$ARCH" in
        Linux/x86_64|Linux/amd64) echo uv-x86_64-unknown-linux-gnu.tar.gz ;;
        Linux/aarch64|Linux/arm64) echo uv-aarch64-unknown-linux-gnu.tar.gz ;;
        Darwin/x86_64) echo uv-x86_64-apple-darwin.tar.gz ;;
        Darwin/arm64|Darwin/aarch64) echo uv-aarch64-apple-darwin.tar.gz ;;
        *) return 1 ;;
    esac
}

UV="$ROOT/.tools/uv/uv"
get_uv() {  # .tools/uv/uv: downloaded from the uv release on GitHub when missing, run only if its SHA-256 matches
    local asset want url part stage got
    if [ -x "$UV" ] && "$UV" --version 2>/dev/null | grep -q "^uv $UV_VERSION"; then
        ok "uv $UV_VERSION in .tools/uv"
        return 0
    fi
    if ! asset="$(uv_asset)"; then
        fail "there is no uv build for $OS $ARCH: install Python 3.12 or newer, then run the setup again."
        return 1
    fi
    want="$(uv_sha256 "$asset")"
    url="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$asset"
    part="$ROOT/.tools/uv/$asset.part"
    stage="$ROOT/.tools/uv/stage"
    mkdir -p "$ROOT/.tools/uv" || return 1
    say "  downloading uv $UV_VERSION from GitHub (about 17 MB)"
    if ! download "$url" "$part"; then
        rm -f "$part"
        fail "could not download $url (network or proxy?)."
        return 1
    fi
    got="$(sha256_of "$part")"
    if [ "$got" != "$want" ]; then
        rm -f "$part"
        fail "the uv download has the wrong SHA-256 (got ${got:-nothing}, want $want): not running it."
        return 1
    fi
    rm -rf "$stage"
    mkdir -p "$stage"
    if ! tar -xzf "$part" -C "$stage" --strip-components=1 || [ ! -f "$stage/uv" ]; then
        rm -rf "$stage" "$part"
        fail "could not unpack the uv download."
        return 1
    fi
    mv -f "$stage/uv" "$UV" && chmod +x "$UV"
    rm -rf "$stage" "$part"
    did "uv $UV_VERSION in .tools/uv (SHA-256 checked)"
}

if venv_works; then
    ok ".venv works (Python $("$VPY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null))"
else
    VPY=""
    if [ -e .venv ]; then
        if [ "$CHECK" = 1 ]; then
            note ".venv is there but does not work (or is older than Python 3.12): the setup makes a new one"
        else
            note ".venv is there but does not work (or is older than Python 3.12): making a new one"
            rm -rf .venv
        fi
    fi
    USE_UV=0
    if find_python; then
        ok "Python $PY_VERSION ($PY) has venv"
        if [ "$CHECK" = 1 ]; then
            note "the setup makes .venv with it"
        elif "$PY" -m venv .venv; then
            did "made .venv"
        else
            note "python -m venv did not work: using uv instead"
            rm -rf .venv
            USE_UV=1
        fi
    else
        note "no Python 3.12 or newer with a venv module on PATH${PY_SEEN:+ (found: ${PY_SEEN#, })}"
        USE_UV=1
    fi
    if [ "$USE_UV" = 1 ]; then
        if [ "$CHECK" = 1 ]; then
            note "the setup downloads uv $UV_VERSION (about 17 MB, SHA-256 checked) and lets it fetch Python 3.12"
        elif get_uv; then
            # uv keeps its Python and cache in .tools/uv too: deleting the folder removes everything
            if UV_PYTHON_INSTALL_DIR="$ROOT/.tools/uv/python" UV_CACHE_DIR="$ROOT/.tools/uv/cache" UV_NO_CONFIG=1 \
                "$UV" venv --seed --python 3.12 .venv; then
                did "made .venv with uv (Python 3.12)"
            else
                fail "uv could not make .venv (see above)."
                rm -rf .venv
            fi
        fi
    fi
    if [ "$CHECK" = 0 ] && ! venv_works; then
        fail "there is no working Python 3.12 here."
        finish Look "The setup needs Python 3.12 or newer: install it (python.org, or your package manager), then run the setup again."
    fi
fi
PYX="${VPY:-$PY}"  # the Python the harness runs with (in check mode, maybe the one on PATH)

# --- 3. packages ------------------------------------------------------------------------------------------------
step 3 "Python packages"
MARK="$ROOT/.venv/.workshop-packages"
WANT_PKGS="$SDK_PIN $ZIG_PIN"
PKG_FAILED=0
if [ -n "$VPY" ] && [ -f "$MARK" ] && [ "$(cat "$MARK" 2>/dev/null)" = "$WANT_PKGS" ]; then
    ok "$SDK_PIN and $ZIG_PIN are installed"
elif [ "$CHECK" = 1 ]; then
    note "not installed yet: the setup installs $SDK_PIN and $ZIG_PIN into .venv (about 110 MB)"
else
    say "  installing $SDK_PIN and $ZIG_PIN (about 110 MB; most of it is the zig C compiler)"
    rm -f "$MARK"
    # the mark is written only after pip succeeded: a run stopped here installs everything again next time
    if "$VPY" -m pip install --disable-pip-version-check --retries 1 --timeout 30 --force-reinstall \
        "$SDK_PIN" "$ZIG_PIN"; then
        printf '%s\n' "$WANT_PKGS" >"$MARK"
        did "packages installed"
    else
        fail "pip could not install the packages (network or proxy?). Run the setup again later: it retries."
        PKG_FAILED=1
    fi
fi

# --- 4. Copilot CLI ------------------------------------------------------------------------------------------------
step 4 "Copilot CLI"
CLI=""
find_cli() {  # sets CLI: the Copilot CLI the harness will use
    local found
    CLI=""
    if [ -n "${COPILOT_CLI_PATH:-}" ] && [ -f "$COPILOT_CLI_PATH" ]; then CLI="$COPILOT_CLI_PATH"; return 0; fi
    if [ -n "$PYX" ] && [ -d harness ]; then  # what the harness finds itself (npm install, a native binary on PATH)
        found="$("$PYX" -c 'from harness.envdetect import find_copilot_cli as f; print(f() or "")' 2>/dev/null | tail -n 1)"
        if [ -n "$found" ]; then CLI="$found"; return 0; fi
    fi
    if [ -n "${HOME:-}" ] && [ -x "$HOME/.local/bin/copilot" ]; then  # where GitHub's install script puts it
        CLI="$HOME/.local/bin/copilot"
        export COPILOT_CLI_PATH="$CLI"  # for the doctor and the console started below
        return 0
    fi
    return 1
}
LOCAL_CLI_DIR="$ROOT/.tools/copilot"   # the pinned CLI, inside this folder: the harness looks here first
local_cli() {  # the pinned CLI in this folder, when it is there
    local c
    for c in "$LOCAL_CLI_DIR"/node_modules/@github/copilot-*/copilot.exe "$LOCAL_CLI_DIR"/node_modules/@github/copilot-*/copilot              "$LOCAL_CLI_DIR/bin/copilot"; do
        if [ -f "$c" ]; then echo "$c"; return 0; fi
    done
    return 1
}
CLI_SHA512_WIN_X64="npcsVKICWbflvURVqkKD2CXx8ofbL3Odj/JUXi4vPbf/u75+oS2YLtWbYnx9l3Xc0Ejm3maFs7r5aww67ikgyw=="
install_cli_windows() {  # npm, else the CLI's own package from the npm registry (no Node.js); winget offers only newer ones
    if command -v npm >/dev/null 2>&1; then
        say "  installing Copilot CLI $COPILOT_VERSION into .tools/copilot with npm (about 130 MB)"
        if npm install --prefix "$LOCAL_CLI_DIR" --no-fund --no-audit "@github/copilot@$COPILOT_VERSION" </dev/null; then
            return 0
        fi
        say "  npm could not install it"
    fi
    local pkg="copilot-win32-x64" tgz="$ROOT/.tools/copilot-win32-x64.tgz" got
    say "  downloading Copilot CLI $COPILOT_VERSION ($pkg, about 60 MB) into .tools/copilot"
    mkdir -p "$ROOT/.tools" "$LOCAL_CLI_DIR/node_modules/@github/$pkg"
    if download "https://registry.npmjs.org/@github/$pkg/-/$pkg-$COPILOT_VERSION.tgz" "$tgz"; then
        got="$(openssl dgst -sha512 -binary "$tgz" 2>/dev/null | base64 -w0 2>/dev/null)"
        if [ "$got" = "$CLI_SHA512_WIN_X64" ] && tar -xzf "$tgz" -C "$LOCAL_CLI_DIR/node_modules/@github/$pkg" --strip-components=1; then
            rm -f "$tgz"
            return 0
        fi
        say "  the download has the wrong sha512 or would not unpack: not using it"
        rm -f "$tgz"
    fi
    if command -v winget >/dev/null 2>&1; then
        say "  installing the Copilot CLI with winget, for your user only (winget has no $COPILOT_VERSION:"
        say "  it installs its newest one, which may not work with the harness; install Node.js and run the setup again)"
        if winget install --id GitHub.Copilot -e --source winget --scope user --accept-source-agreements                 --accept-package-agreements --disable-interactivity </dev/null; then return 0; fi
        say "  winget could not install it"
    fi
    say "  neither npm (Node.js) nor winget is here"
    return 1
}
install_cli() {
    local script="$ROOT/.tools/copilot-install.sh" got
    case "$OS" in
        Linux|Darwin) ;;
        MINGW*|MSYS*|CYGWIN*) install_cli_windows; return ;;
        *) say "  this script installs the Copilot CLI only on Linux, macOS and Windows"; return 1 ;;
    esac
    mkdir -p "$ROOT/.tools" || return 1
    say "  installing Copilot CLI $COPILOT_VERSION into .tools/copilot with GitHub's install script (about 130 MB)"
    if ! download "$COPILOT_INSTALL_URL" "$script.part"; then
        rm -f "$script.part"
        say "  could not download GitHub's install script"
        return 1
    fi
    got="$(sha256_of "$script.part")"
    if [ "$got" != "$COPILOT_INSTALL_SHA256" ]; then
        rm -f "$script.part"
        say "  GitHub's install script has the wrong SHA-256: not running it"
        return 1
    fi
    mv -f "$script.part" "$script"
    # run, never piped; the prefix's bin on PATH keeps the script from asking to edit a shell profile;
    # an empty GITHUB_TOKEN keeps a token of yours from being sent with the download
    GITHUB_TOKEN="" VERSION="$COPILOT_VERSION" PREFIX="$LOCAL_CLI_DIR" PATH="$LOCAL_CLI_DIR/bin:$PATH"         bash "$script" </dev/null || return 1
    [ -x "$LOCAL_CLI_DIR/bin/copilot" ]
}

# The harness needs exactly Copilot CLI $COPILOT_VERSION: a newer CLI speaks another protocol than the pinned SDK. So
# the setup installs that version into this folder, whatever CLI you have already, and the harness starts it with
# auto-update off. Your own CLI stays as it is.
if local_cli >/dev/null; then
    find_cli
    ok "Copilot CLI $COPILOT_VERSION in .tools/copilot: $CLI"
elif [ "$CHECK" = 1 ]; then
    note "not here yet: the setup installs Copilot CLI $COPILOT_VERSION into .tools/copilot"
elif install_cli && local_cli >/dev/null && find_cli; then
    did "Copilot CLI $COPILOT_VERSION in .tools/copilot: $CLI"
elif find_cli; then
    note "Copilot CLI $COPILOT_VERSION could not be installed here; the harness uses $CLI, which may be too new for it"
else
    CLI=""
    fail "the Copilot CLI is not installed, so you stay on the fake model. Later: https://gh.io/install-copilot-readme, then run the setup again."
fi
CLI_CMD="copilot"  # how to start the CLI in a terminal (the login hint at the end)
case "$CLI" in *.tools[/\\]copilot[/\\]*) CLI_CMD="\"$CLI\" --no-auto-update" ;; esac
if [ -n "$CLI" ] && [ "$CLI" = "${HOME:-}/.local/bin/copilot" ]; then
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *)
            CLI_CMD="~/.local/bin/copilot"
            note "~/.local/bin is not on your PATH: the setup tells the harness where the CLI is. In a terminal, run it as ~/.local/bin/copilot." ;;
    esac
fi

# --- 5. editor -------------------------------------------------------------------------------------------------------
step 5 "code editor for the console"
MONACO="console/scripts/vendor_monaco.py"
if [ ! -f "$MONACO" ]; then
    note "no console/ folder here: skipped"
elif [ -z "$PYX" ]; then
    note "needs Python 3.12: skipped"
elif "$PYX" "$MONACO" --check >/dev/null 2>&1; then
    ok "the Monaco editor is installed"
elif [ "$CHECK" = 1 ]; then
    note "not installed: the setup downloads the Monaco editor (about 5 MB, checked)"
elif "$PYX" "$MONACO"; then
    did "the Monaco editor is installed"
else
    note "the editor could not be downloaded: the console uses a plain text box. Run the setup again later."
fi

# --- 6. gates ----------------------------------------------------------------------------------------------------
step 6 "gates (no Docker)"
# Docker is never used: the gates run with the zig C compiler (the Docker-free ladder). The console reads the ladder
# from .tools/ladder, once, at its start.
LADDER=lite
if [ "$CHECK" = 0 ] && mkdir -p "$ROOT/.tools" 2>/dev/null; then
    printf '%s\n' "$LADDER" > "$ROOT/.tools/ladder" 2>/dev/null || note "could not write .tools/ladder"
fi
ok "the gates run without Docker (no Docker needed)"

# macOS ships bash 3.2; a check of the Docker-free ladder needs bash 4 or later and is skipped with an older one. The
# bash it runs with is the harness's: HARNESS_BASH, else bash on PATH.
if [ "$OS" = Darwin ] && [ "$LADDER" = lite ] &&
    ! "${HARNESS_BASH:-bash}" -c 'test "${BASH_VERSINFO[0]}" -ge 4' >/dev/null 2>&1; then
    note "macOS: install a newer bash (brew install bash) so every gate runs; until then a check is skipped"
fi

# --- 7. doctor --------------------------------------------------------------------------------------------------------
step 7 "doctor"
if [ ! -d harness ]; then
    fail "there is no harness/ folder: is this the workshop repository?"
    finish Look "Download the workshop repository again and run the setup in its folder."
fi
if [ -z "$PYX" ]; then
    fail "the doctor needs Python 3.12 or newer"
    finish Look "Run the setup without --check: it gets Python 3.12."
fi
DOCTOR_OUT="$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/setup-doctor.$$")"
HIDE_DOCKER="DOCKER_HOST=unix:///nonexistent/tinystream-no-docker.sock"
DOCTOR_ENV="$HIDE_DOCKER"  # the doctor checks the Docker-free ladder, whatever Docker there is
DEPTH=""
run_doctor() {  # run_doctor SECONDS [--skip-auth]: sets DEPTH from the doctor's "Depth:" line
    local secs="$1" rc
    shift
    run_limited "$secs" "$DOCTOR_OUT" env $DOCTOR_ENV PYTHONIOENCODING=utf-8 "$PYX" -m harness doctor --depth ${1+"$@"}
    rc=$?
    grep -v -e '^Depth: ' -e '^Runtime mode:' "$DOCTOR_OUT"  # the runtime line names the facilitator's runner
    DEPTH="$(sed -n 's/^Depth: //p' "$DOCTOR_OUT" | tail -n 1 | tr -d '\r')"
    if ! printf '%s\n' "$DEPTH" | grep -Eq '^(Try|Full) \((fake model|your Copilot)\)$'; then
        DEPTH=""
        if [ "$rc" = 124 ]; then
            say "  the doctor did not finish within $secs s"
        else
            say "  the doctor gave no depth (exit code $rc)"
        fi
    fi
}
if [ -n "$CLI" ]; then
    say "  checking your Copilot login: this can take up to 5 minutes; please wait, do not close the window"
    run_doctor "$DOCTOR_TIMEOUT"
else
    run_doctor "$DOCTOR_TIMEOUT" --skip-auth
fi
# no answer: once more without what can hang (the login check, the Docker probes), so it ends at Try, not at Look
RETRY_WITHOUT=""
[ -n "$CLI" ] && RETRY_WITHOUT="the login check"
if [ -z "$DEPTH" ] && [ -n "$RETRY_WITHOUT" ]; then
    say "  once more, without $RETRY_WITHOUT:"
    DOCTOR_ENV="$HIDE_DOCKER"
    run_doctor 120 --skip-auth
fi
rm -f "$DOCTOR_OUT"
if [ -z "$DEPTH" ]; then
    finish Look "The doctor could not run, so the setup cannot tell what works here. Fix what the lines above say, or ask for help."
fi
case "$DEPTH" in Full*) DEPTH="Try${DEPTH#Full}" ;; esac  # Full needs Docker, which the setup never uses
WHY=""
if [ "$PKG_FAILED" = 1 ]; then
    WHY="Some Python packages are missing: run the setup again when the network works."
elif [ -n "$CLI" ]; then
    case "$DEPTH" in *"(fake model)") WHY="To use your own Copilot: run $CLI_CMD, type /login, then run the setup again." ;; esac
fi

# --- 8. console ---------------------------------------------------------------------------------------------------------
step 8 "Harness Console"
if [ "$CHECK" = 1 ]; then
    note "not started (--check)"
elif [ "$NO_CONSOLE" = 1 ]; then
    note "not started (--no-console)"
elif [ ! -d console/console ]; then
    note "no console/ folder here: skipped"
else
    mkdir -p "$ROOT/.tools"
    LOG="$ROOT/.tools/console.log"
    if [ -f "$LOG" ]; then mv -f "$LOG" "$LOG.old"; fi  # a console that runs keeps writing the old one
    # in the background, so it keeps running after this script ends; a second start prints the running one
    (
        cd "$ROOT/console" || exit 1
        export PYTHONIOENCODING=utf-8
        exec nohup "$VPY" -m console serve --personal
    ) >"$LOG" 2>&1 </dev/null &
    CPID=$!
    URL=""
    ticks=0
    while [ "$ticks" -lt $((CONSOLE_WAIT * 5)) ]; do
        URL="$(grep -aEo 'http://(127\.0\.0\.1|localhost):[0-9]+' "$LOG" 2>/dev/null | head -n 1)"
        [ -n "$URL" ] && break
        kill -0 "$CPID" 2>/dev/null || break
        sleep 0.2
        ticks=$((ticks + 1))
    done
    [ -n "$URL" ] || URL="$(grep -aEo 'http://(127\.0\.0\.1|localhost):[0-9]+' "$LOG" 2>/dev/null | head -n 1)"
    if [ -n "$URL" ]; then
        if grep -aq "already running" "$LOG" 2>/dev/null; then
            ok "the console is already running: $URL"
        else
            ok "the console runs in the background: $URL"
        fi
        STOP_PID="$("$VPY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["pid"])' \
            "$ROOT/.console-data/serve.json" 2>/dev/null)"
        if [ -n "$STOP_PID" ]; then
            say "        open it in your browser; to stop it: kill $STOP_PID (log: .tools/console.log)"
        else
            say "        open it in your browser (log: .tools/console.log)"
        fi
    elif kill -0 "$CPID" 2>/dev/null; then
        note "the console is still starting: its address will be in .tools/console.log"
    else
        sed 's/^/        /' "$LOG" | tail -n 8
        note "the console did not start (log: .tools/console.log). The harness works without it."
    fi
fi

finish "$DEPTH" "$WHY"
