# The stage's terminals. Once per terminal (Git Bash), in the repository root:
#
#     source workshop/stage-shell.sh
#
# On Windows without Git Bash at hand (a participant's laptop, where the setup brought a portable git), workshop\shell
# opens such a terminal from PowerShell or cmd.
#
# It takes the terminal into the console's copy of this repository, .console-data/units/unit-01/repo: the folder the
# console's Today runs its commands in, and the one its Code, Runs and "Check my code" pages show. So whatever a command
# typed here changes, the console shows too: one place for the whole stage. It sets what the commands need there, the
# same as the console gives its own commands (console/console/workshop.py):
#
#   TS_LITE  MSYS_NO_PATHCONV=1     the ladder: TS_LITE as given; else 1 (lite, Docker hidden) when the setup wrote
#                                   lite in .tools/ladder and there is no tinystream-dev image (a participant's
#                                   laptop), 0 (full) otherwise (the stage); Git Bash leaves the arguments alone
#   PATH                            this repository's .venv first: python is the venv's
#   WORKSHOP                        this repository's workshop/ folder: the patches, day-numbers.py ...
#                                   (the copy has no workshop/ on a ticket branch)
#   HARNESS_HIDDEN_TESTS            this repository's hidden-tests/: the referee of the experiments (never in the copy,
#                                   so an agent working there cannot read the held-back tests)
#   TS_ROOT                         this repository's root:  cd "$TS_ROOT"  goes back there
#
# Sourcing it again (from anywhere: source "$WORKSHOP/stage-shell.sh") only goes back into the copy.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "source it, do not run it:   source workshop/stage-shell.sh"
    exit 2
fi
_ts_here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"                      # /c/... : for PATH
_ts_root="$(cd "$_ts_here" && { pwd -W 2>/dev/null || pwd; })"                   # C:/... : for python and git too
if [ ! -d "$_ts_here/.console-data/units/unit-01/repo" ]; then
    echo "no console copy yet in $_ts_root: start the console once (bash setup.sh), stop it, then bash workshop/stage-unit.sh"
    unset _ts_here _ts_root
    return 1
fi
_ts_lite="${TS_LITE:-}"
if [ -z "$_ts_lite" ]; then
    _ts_lite=0
    if [ "$(cat "$_ts_here/.tools/ladder" 2>/dev/null)" = lite ] &&
        ! { command -v docker >/dev/null 2>&1 && docker image inspect tinystream-dev:latest >/dev/null 2>&1; }; then
        _ts_lite=1
    fi
fi
export TS_LITE="$_ts_lite" MSYS_NO_PATHCONV=1 TS_ROOT="$_ts_root" WORKSHOP="$_ts_root/workshop"
if [ "$_ts_lite" = 1 ]; then   # as the console does: no run reaches for Docker (console/console/ladder.py)
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) export DOCKER_HOST="npipe:////./pipe/tinystream_no_docker" ;;
        *) export DOCKER_HOST="unix:///nonexistent/tinystream-no-docker.sock" ;;
    esac
fi
if [ -f "$_ts_here/hidden-tests/run.sh" ]; then export HARNESS_HIDDEN_TESTS="$_ts_root/hidden-tests"; fi
for _ts_venv in "$_ts_here/.venv/Scripts" "$_ts_here/.venv/bin"; do
    if [ -d "$_ts_venv" ]; then
        case ":$PATH:" in *":$_ts_venv:"*) ;; *) export PATH="$_ts_venv:$PATH" ;; esac
        break
    fi
done
for _ts_cli in "$_ts_here"/.tools/copilot/node_modules/@github/copilot-*/ "$_ts_here/.tools/copilot/bin/"; do
    if [ -d "$_ts_cli" ]; then       # the pinned CLI the setup installed: `copilot` in this terminal is that one
        case ":$PATH:" in *":${_ts_cli%/}:"*) ;; *) export PATH="${_ts_cli%/}:$PATH" ;; esac
        break
    fi
done
export COPILOT_AUTO_UPDATE=false     # never the newer CLI it would download by itself
cd "$_ts_here/.console-data/units/unit-01/repo" || return 1
echo "stage terminal: in the console's copy ($(git rev-parse --abbrev-ref HEAD 2>/dev/null) at $(git log --oneline -1 2>/dev/null | cut -c1-60))"
echo "  WORKSHOP=$WORKSHOP   back to the root: cd \"\$TS_ROOT\""
if [ "$TS_LITE" = 1 ]; then echo "  lite ladder: no Docker here, the checks that need it say not run here"; fi
unset _ts_here _ts_root _ts_venv _ts_lite
