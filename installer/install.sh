#!/bin/sh
# pdms installer for macOS and Linux.
#
#   curl -LsSf https://github.com/lianabeatriz93/pdms-cli/releases/latest/download/install.sh | sh
#
# Options (environment variables):
#   PDMS_VERSION=0.2.0   install that version instead of the latest release
#   PDMS_WHEEL=<path|url> install that package file instead of downloading a release (used by CI)
set -eu

REPO="lianabeatriz93/pdms-cli"

say() { printf '%s\n' "$*"; }
fail() { printf 'pdms installer: error: %s\n' "$*" >&2; exit 1; }

find_uv() {
    if command -v uv >/dev/null 2>&1; then command -v uv; return 0; fi
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then echo "$candidate"; return 0; fi
    done
    return 1
}

command -v curl >/dev/null 2>&1 || fail "curl is required"

UV="$(find_uv || true)"
if [ -z "$UV" ]; then
    say "Installing uv (https://docs.astral.sh/uv/)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV="$(find_uv || true)"
    [ -n "$UV" ] || fail "uv was installed but cannot be found; open a new terminal and run the installer again"
fi

if [ -n "${PDMS_WHEEL:-}" ]; then
    SOURCE="$PDMS_WHEEL"
else
    VERSION="${PDMS_VERSION:-}"
    if [ -z "$VERSION" ]; then
        LATEST="$(curl -LsS -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest")"
        case "$LATEST" in
            */tag/*) VERSION="${LATEST##*/tag/}" ;;
            *) fail "could not find the latest release of $REPO" ;;
        esac
    fi
    VERSION="${VERSION#v}"
    SOURCE="https://github.com/$REPO/releases/download/v$VERSION/pdms_cli-$VERSION-py3-none-any.whl"
fi

say "Installing pdms from $SOURCE"
"$UV" tool install --force "$SOURCE"
"$UV" tool update-shell >/dev/null 2>&1 || true

BIN_DIR="$("$UV" tool dir --bin)"
"$BIN_DIR/pdms" --version || fail "pdms was installed but does not start"

if ! command -v pdms >/dev/null 2>&1; then
    say "Open a new terminal to use pdms (it is installed in $BIN_DIR)."
fi
if ! command -v poetry >/dev/null 2>&1; then
    say "Note: running PDMS services also needs Poetry: https://python-poetry.org/docs/#installation"
fi
say "Done. Next: pdms --help  -  tab completion: pdms --install-completion  -  updates: pdms self-update"
