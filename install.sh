#!/usr/bin/env bash
# Run inside the WSL distribution that will execute Codex tasks.
set -euo pipefail
umask 077

BRIDGE_SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BRIDGE_INSTALL_DIR="${BRIDGE_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/antigravity-codex}"
BRIDGE_CODEX_VERSION="${BRIDGE_CODEX_VERSION:-0.169.0}"
BRIDGE_PYTHON_VERSION="${BRIDGE_PYTHON_VERSION:-3.12.14}"
BRIDGE_UV_VERSION="${BRIDGE_UV_VERSION:-0.12.5}"

if [[ "${1:-}" == '--help' || "${1:-}" == '-h' ]]; then
  cat <<'HELP'
Usage: bash install.sh [--wsl-only | --windows-home /mnt/c/Users/NAME]
                       [--workspace-root /path/to/projects] [--replace-existing]

Installs a separate Codex execution environment and merges Antigravity config.
Run as your regular WSL user, not with sudo. Existing Codex logins are not copied.

Options are passed to scripts/configure.py; --workspace-root can be repeated.
--replace-existing permits replacing an existing, different codex-worker entry.
Without --wsl-only, the Windows user profile is detected automatically.

Optional environment: BRIDGE_INSTALL_DIR, BRIDGE_CODEX_VERSION,
BRIDGE_PYTHON_VERSION, BRIDGE_UV_VERSION. The defaults pin tested versions.
HELP
  exit 0
fi

BRIDGE_CONFIG_ARGS=("$@")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --wsl-only|--replace-existing) shift ;;
    --windows-home|--workspace-root|--wsl-config-dir)
      [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || {
        echo "Missing value for $1" >&2; exit 1;
      }
      shift 2 ;;
    *) echo "Unknown option: $1 (see bash install.sh --help)" >&2; exit 1 ;;
  esac
done
set -- "${BRIDGE_CONFIG_ARGS[@]}"

if [[ "$(uname -s)" != Linux || -z "${WSL_DISTRO_NAME:-}" ]]; then
  echo 'Please run this installer inside WSL (Debian or Ubuntu).' >&2
  exit 1
fi
if [[ "$(id -u)" == 0 ]]; then
  echo 'Run as your regular WSL user. Use sudo only for the prerequisite apt command.' >&2
  exit 1
fi
case "$BRIDGE_INSTALL_DIR" in
  /*) ;;
  *) echo 'BRIDGE_INSTALL_DIR must be an absolute Linux path.' >&2; exit 1 ;;
esac
case "$BRIDGE_INSTALL_DIR" in
  /|/home|/root|/usr|/usr/*|/etc|/etc/*|/bin|/bin/*|"$HOME"|"$HOME/.codex"|"$HOME/.codex/"*)
    echo 'Use a dedicated installation directory, not an existing application or system directory.' >&2
    exit 1 ;;
esac
for version in "$BRIDGE_CODEX_VERSION" "$BRIDGE_PYTHON_VERSION" "$BRIDGE_UV_VERSION"; do
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo 'Versions must use the x.y.z format.' >&2; exit 1;
  }
done

# Keep Windows executables and personal login-shell settings out of bootstrap.
export PATH="$BRIDGE_INSTALL_DIR/bin:$BRIDGE_INSTALL_DIR/runtime/uv:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
for dependency in curl tar git rg flock; do
  command -v "$dependency" >/dev/null || {
    echo 'Missing prerequisites. Run: sudo apt update && sudo apt install -y curl ca-certificates git ripgrep tar util-linux' >&2
    exit 1
  }
done

mkdir -p "$BRIDGE_INSTALL_DIR"
chmod 700 "$BRIDGE_INSTALL_DIR"
exec 9>"$BRIDGE_INSTALL_DIR/.install.lock"
flock -n 9 || { echo 'Another installation is running.' >&2; exit 1; }

# Validate configuration and refuse upgrades while a task is active, before
# changing an existing runtime. A new installation is validated after bootstrap.
if [[ -x "$BRIDGE_INSTALL_DIR/venv/bin/python" ]]; then
  BRIDGE_CURRENT_PYTHON="$("$BRIDGE_INSTALL_DIR/venv/bin/python" -c 'import platform; print(platform.python_version())')"
  if [[ "$BRIDGE_CURRENT_PYTHON" != "$BRIDGE_PYTHON_VERSION" ]]; then
    echo 'The existing runtime uses a different Python version. Keep that version or choose a fresh BRIDGE_INSTALL_DIR.' >&2
    exit 1
  fi
  "$BRIDGE_INSTALL_DIR/venv/bin/python" "$BRIDGE_SOURCE_DIR/scripts/configure.py" \
    --install-dir "$BRIDGE_INSTALL_DIR" --check "$@"
fi

BRIDGE_DOWNLOAD_DIR="$(mktemp -d)"
trap 'rm -rf -- "$BRIDGE_DOWNLOAD_DIR"' EXIT
mkdir -p "$BRIDGE_INSTALL_DIR/runtime/uv" "$BRIDGE_INSTALL_DIR/codex-home"
chmod 700 "$BRIDGE_INSTALL_DIR/codex-home"
curl --fail --silent --show-error --location --retry 2 --connect-timeout 20 --max-time 180 \
  "https://astral.sh/uv/$BRIDGE_UV_VERSION/install.sh" -o "$BRIDGE_DOWNLOAD_DIR/uv.sh"
env UV_UNMANAGED_INSTALL="$BRIDGE_INSTALL_DIR/runtime/uv" sh "$BRIDGE_DOWNLOAD_DIR/uv.sh"

export UV_PYTHON_INSTALL_DIR="$BRIDGE_INSTALL_DIR/runtime/python"
export UV_CACHE_DIR="$BRIDGE_INSTALL_DIR/runtime/uv-cache"
BRIDGE_UV="$BRIDGE_INSTALL_DIR/runtime/uv/uv"
"$BRIDGE_UV" python install --no-config --no-bin "$BRIDGE_PYTHON_VERSION"
if [[ ! -x "$BRIDGE_INSTALL_DIR/venv/bin/python" ]]; then
  "$BRIDGE_UV" venv --no-config --managed-python --python "$BRIDGE_PYTHON_VERSION" "$BRIDGE_INSTALL_DIR/venv"
fi
"$BRIDGE_UV" pip sync --no-config --python "$BRIDGE_INSTALL_DIR/venv/bin/python" "$BRIDGE_SOURCE_DIR/requirements.lock.txt"

"$BRIDGE_INSTALL_DIR/venv/bin/python" "$BRIDGE_SOURCE_DIR/scripts/configure.py" \
  --install-dir "$BRIDGE_INSTALL_DIR" --check "$@"

# The official installer verifies its release archives. A pinned release avoids
# implicit CLI upgrades. The dedicated bin directory is already on this PATH,
# so the installer does not need to alter the user's shell profile.
curl --fail --silent --show-error --location --retry 2 --connect-timeout 20 --max-time 180 \
  https://chatgpt.com/codex/install.sh -o "$BRIDGE_DOWNLOAD_DIR/codex.sh"
env CODEX_HOME="$BRIDGE_INSTALL_DIR/codex-home" \
  CODEX_INSTALL_DIR="$BRIDGE_INSTALL_DIR/bin" CODEX_NON_INTERACTIVE=true \
  sh "$BRIDGE_DOWNLOAD_DIR/codex.sh" --release "$BRIDGE_CODEX_VERSION"

"$BRIDGE_INSTALL_DIR/venv/bin/python" "$BRIDGE_SOURCE_DIR/scripts/configure.py" \
  --install-dir "$BRIDGE_INSTALL_DIR" "$@"
"$BRIDGE_INSTALL_DIR/venv/bin/python" "$BRIDGE_INSTALL_DIR/app/bridge.py" health
printf '\nInstallation complete. Log in to this independent Codex environment:\n  %q login --device-auth\n' \
  "$BRIDGE_INSTALL_DIR/bin/codex-worker"
printf 'Then restart Antigravity and ask Gemini to call codex-worker/codex_health.\n'
