#!/bin/sh
set -eu

main() {
    base='@POTETO_BASE_URL@'
    version='@POTETO_VERSION@'
    dir=${POTETO_INSTALL_DIR:-"$HOME/.local/bin"}
    if ! command -v python3 >/dev/null 2>&1 ||
       ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
        printf '%s\n' 'Poteto requires Python 3.10 or newer.' >&2
        exit 1
    fi
    mkdir -p "$dir"
    if [ -d "$dir/poteto" ]; then
        printf '%s\n' "$dir/poteto is a directory; choose another POTETO_INSTALL_DIR." >&2
        exit 1
    fi
    tmp=$(mktemp "$dir/.poteto.XXXXXX")
    trap 'rm -f "$tmp"' EXIT
    trap 'exit 1' HUP INT TERM
    curl -fsSL --proto '=https' --proto-redir '=https' "$base/poteto-$version.pyz" -o "$tmp"
    if [ "$(python3 "$tmp" --version)" != "poteto $version" ]; then
        printf '%s\n' 'Downloaded Poteto did not report the expected version.' >&2
        exit 1
    fi
    chmod 0755 "$tmp"
    mv -f "$tmp" "$dir/poteto"
    printf 'Installed Poteto %s at %s/poteto\n' "$version" "$dir"
    case ":${PATH:-}:" in
        *":$dir:"*) printf '%s\n' 'Run: poteto login' ;;
        *) printf 'Run: "%s/poteto" login\n' "$dir" ;;
    esac
}

main "$@"
