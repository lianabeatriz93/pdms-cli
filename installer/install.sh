#!/bin/sh
# pdms installer for macOS and Linux.
#
#   curl -LsSf https://github.com/lianabeatriz93/pdms-cli/releases/latest/download/install.sh | sh
#
# Options (environment variables):
#   PDMS_VERSION=0.2.0   install that version instead of the latest release
#   PDMS_PRERELEASE=1    install the latest release including alpha/beta pre-releases
#   PDMS_WHEEL=<path|url> install that package file instead of downloading a release (used by CI)
#   PDMS_MENU=1|0        add pdms to the app menu (pdms ui --install) without asking, or do not
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
    if [ -z "$VERSION" ] && [ -n "${PDMS_PRERELEASE:-}" ]; then
        # The API lists every release (pre-releases included), newest first.
        VERSION="$(curl -LsS "https://api.github.com/repos/$REPO/releases?per_page=1" \
            | grep -o '"tag_name": *"[^"]*"' | head -n 1 | sed 's/.*"\([^"]*\)"$/\1/')"
        [ -n "$VERSION" ] || fail "could not find the latest pre-release of $REPO"
    fi
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
# Without --python uv may take an older Python it finds first (macOS's /usr/bin/python3 is 3.9).
"$UV" tool install --force --python ">=3.10" "$SOURCE"
"$UV" tool update-shell >/dev/null 2>&1 || true

BIN_DIR="$("$UV" tool dir --bin)"
"$BIN_DIR/pdms" --version || fail "pdms was installed but does not start"

# The app menu entry (pdms ui --install). Piped into sh, the question goes to the terminal itself.
MENU="${PDMS_MENU:-}"
if [ -z "$MENU" ] && [ -r /dev/tty ] && [ -w /dev/tty ]; then
    printf 'Add pdms to the app menu (pdms ui in a window of its own)? [Y/n] ' > /dev/tty
    read -r answer < /dev/tty || answer=n
    case "$answer" in [nN]*) MENU=0 ;; *) MENU=1 ;; esac
fi
if [ "$MENU" = "1" ]; then
    "$BIN_DIR/pdms" ui --install || say "Could not add pdms to the app menu; try later with: pdms ui --install"
fi

if ! command -v pdms >/dev/null 2>&1; then
    say "Open a new terminal to use pdms (it is installed in $BIN_DIR)."
fi
if ! command -v poetry >/dev/null 2>&1; then
    say "Note: running PDMS services also needs Poetry: https://python-poetry.org/docs/#installation"
fi
say "Done. Next: pdms --help  -  tab completion: pdms --install-completion  -  updates: pdms self-update"
