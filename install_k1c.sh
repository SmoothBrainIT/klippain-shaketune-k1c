#!/bin/sh
# Shake&Tune K1C Install Script
#
# Installs klippain-shaketune on a rooted Creality K1C printer.
# Requires: Guilouz helper script already installed (Klipper + Moonraker running).
#
# Unlike the standard install.sh, this script:
#   - Does NOT require apt/dpkg (K1C has no apt)
#   - Does NOT install numpy/matplotlib (graph generation runs on a remote Docker server)
#   - Auto-detects the Klipper extras directory and Python interpreter
#   - Uses the K1C init system (/etc/init.d/S55klipper_service)
#
# Usage:
#   sh install_k1c.sh
#
# Environment overrides (optional):
#   KLIPPER_EXTRAS_DIR   Force a specific Klipper extras path
#   PYTHON_BIN           Force a specific Python interpreter (e.g. /usr/lib/python3.8)
#   K_SHAKETUNE_PATH     Override install directory (default: /usr/data/klippain_shaketune)

set -e
export LC_ALL=C

K_SHAKETUNE_PATH="${K_SHAKETUNE_PATH:-/usr/data/klippain_shaketune}"
PRINTER_DATA_CONFIG="/usr/data/printer_data/config"
MOONRAKER_CONFIG="${PRINTER_DATA_CONFIG}/moonraker.conf"
FORK_URL="https://github.com/SmoothBrainIT/klippain-shaketune-k1c.git"

printf "\n============================================\n"
printf "  Klippain Shake&Tune - K1C Install Script  \n"
printf "============================================\n\n"

# ---------------------------------------------------------------------------
# 1. Detect Klipper extras directory (try writable candidates in order)
# ---------------------------------------------------------------------------
detect_klipper_extras() {
    if [ -n "${KLIPPER_EXTRAS_DIR:-}" ]; then
        if [ -d "$KLIPPER_EXTRAS_DIR" ]; then
            echo "$KLIPPER_EXTRAS_DIR"
            return 0
        fi
        echo "[ERROR] KLIPPER_EXTRAS_DIR='$KLIPPER_EXTRAS_DIR' does not exist." >&2
        return 1
    fi

    for candidate in \
        /usr/data/klipper/klippy/extras \
        /usr/share/klipper/klippy/extras; do
        if [ -d "$candidate" ] && touch "$candidate/.shaketune_write_test" 2>/dev/null; then
            rm -f "$candidate/.shaketune_write_test"
            echo "$candidate"
            return 0
        fi
    done

    echo "[ERROR] Could not find a writable Klipper extras directory." >&2
    echo "        Checked: /usr/data/klipper/klippy/extras" >&2
    echo "                 /usr/share/klipper/klippy/extras" >&2
    echo "        Set KLIPPER_EXTRAS_DIR to override." >&2
    return 1
}

# ---------------------------------------------------------------------------
# 2. Detect Python interpreter and pip
# ---------------------------------------------------------------------------
detect_python() {
    if [ -n "${PYTHON_BIN:-}" ]; then
        if "$PYTHON_BIN" --version >/dev/null 2>&1; then
            echo "$PYTHON_BIN"
            return 0
        fi
        echo "[ERROR] PYTHON_BIN='$PYTHON_BIN' not found." >&2
        return 1
    fi

    # Try venv Python first (Guilouz may create one)
    for candidate in \
        /usr/data/klippy-env/bin/python3 \
        /usr/data/klippy-env/bin/python \
        /usr/lib/python3.8/bin/python3.8 \
        /usr/bin/python3 \
        /usr/bin/python3.8; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done

    echo "[ERROR] No Python 3 interpreter found." >&2
    return 1
}

detect_pip() {
    local python="$1"
    for candidate in \
        /usr/data/klippy-env/bin/pip3 \
        /usr/data/klippy-env/bin/pip \
        /usr/bin/pip3 \
        /usr/bin/pip; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    # Fall back to python -m pip
    if "$python" -m pip --version >/dev/null 2>&1; then
        echo "$python -m pip"
        return 0
    fi
    echo "[ERROR] No pip found." >&2
    return 1
}

# ---------------------------------------------------------------------------
# 3. Check Klipper service
# ---------------------------------------------------------------------------
check_klipper_service() {
    if [ -x /etc/init.d/S55klipper_service ]; then
        printf "[PRE-CHECK] Klipper init script found.\n\n"
        return 0
    fi
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet klipper 2>/dev/null; then
        printf "[PRE-CHECK] Klipper systemd service found.\n\n"
        return 0
    fi
    echo "[ERROR] Klipper service not found. Please install Klipper first (use Guilouz Helper Script)."
    exit 1
}

# ---------------------------------------------------------------------------
# 4. Download / update the repository
# ---------------------------------------------------------------------------
download_shaketune() {
    if [ ! -d "${K_SHAKETUNE_PATH}" ]; then
        printf "[DOWNLOAD] Cloning Shake&Tune K1C fork...\n"
        if git clone "${FORK_URL}" "${K_SHAKETUNE_PATH}"; then
            chmod +x "${K_SHAKETUNE_PATH}/install_k1c.sh"
            printf "[DOWNLOAD] Done.\n\n"
        else
            echo "[ERROR] Clone failed. Check your internet connection."
            exit 1
        fi
    else
        printf "[DOWNLOAD] Repository already exists at %s. Pulling latest...\n" "${K_SHAKETUNE_PATH}"
        git -C "${K_SHAKETUNE_PATH}" pull --ff-only
        printf "\n"
    fi
}

# ---------------------------------------------------------------------------
# 5. Install Python dependencies (minimal — no numpy/matplotlib)
# ---------------------------------------------------------------------------
install_python_deps() {
    local pip="$1"
    printf "[SETUP] Installing K1C Python dependencies (zstandard + GitPython)...\n"

    # zstandard: we need a MIPS32-compatible wheel.
    # Try a prebuilt wheel from the fork's releases first, then fall back to pip.
    ZSTD_WHEEL_URL="https://github.com/SmoothBrainIT/klippain-shaketune-k1c/releases/latest/download/zstandard-mips32.whl"
    ZSTD_WHEEL_PATH="/tmp/zstandard_mips32.whl"

    if wget -q -O "${ZSTD_WHEEL_PATH}" "${ZSTD_WHEEL_URL}" 2>/dev/null; then
        printf "[SETUP] Using prebuilt MIPS32 zstandard wheel...\n"
        $pip install --no-deps "${ZSTD_WHEEL_PATH}" || {
            printf "[WARN] Prebuilt wheel install failed, trying PyPI (may fail on MIPS)...\n"
            $pip install "zstandard==0.23.0"
        }
        rm -f "${ZSTD_WHEEL_PATH}"
    else
        printf "[WARN] Could not download MIPS32 wheel, trying PyPI...\n"
        printf "       If this fails, see docs/k1c_setup.md for cross-compilation instructions.\n"
        $pip install "zstandard==0.23.0"
    fi

    # GitPython is pure Python — installs fine from PyPI on MIPS
    $pip install "GitPython==3.1.41"

    printf "[SETUP] Dependencies installed.\n\n"
}

# ---------------------------------------------------------------------------
# 6. Link the shaketune module into Klipper extras
# ---------------------------------------------------------------------------
link_module() {
    local extras_dir="$1"
    local target="${extras_dir}/shaketune"

    if [ -L "$target" ]; then
        printf "[INSTALL] Removing existing shaketune symlink...\n"
        rm "$target"
    elif [ -d "$target" ]; then
        printf "[INSTALL] Removing existing shaketune directory...\n"
        rm -rf "$target"
    fi

    ln -s "${K_SHAKETUNE_PATH}/shaketune" "$target"
    printf "[INSTALL] Linked shaketune -> %s\n\n" "$target"
}

# ---------------------------------------------------------------------------
# 7. Add Moonraker update manager entry
# ---------------------------------------------------------------------------
add_updater() {
    if [ ! -f "$MOONRAKER_CONFIG" ]; then
        printf "[SKIP] moonraker.conf not found at %s — skipping update manager.\n\n" "$MOONRAKER_CONFIG"
        return
    fi

    if grep -q '\[update_manager.*Klippain-ShakeTune-K1C\]' "$MOONRAKER_CONFIG" 2>/dev/null; then
        printf "[INSTALL] Moonraker update manager already present. Skipping.\n\n"
        return
    fi

    printf "[INSTALL] Adding Moonraker update manager entry...\n"
    cat >> "$MOONRAKER_CONFIG" << 'EOF'

## Klippain Shake&Tune K1C automatic update management
[update_manager Klippain-ShakeTune-K1C]
type: git_repo
origin: https://github.com/SmoothBrainIT/klippain-shaketune-k1c.git
path: /usr/data/klippain_shaketune
primary_branch: main
managed_services: klipper
EOF
    printf "[INSTALL] Done.\n\n"
}

# ---------------------------------------------------------------------------
# 8. Restart Klipper
# ---------------------------------------------------------------------------
restart_klipper() {
    printf "[POST-INSTALL] Restarting Klipper...\n"
    if [ -x /etc/init.d/S55klipper_service ]; then
        /etc/init.d/S55klipper_service restart
    elif command -v systemctl >/dev/null 2>&1; then
        systemctl restart klipper
    else
        printf "[WARN] Could not restart Klipper automatically. Please restart it manually.\n"
    fi
}

restart_moonraker() {
    printf "[POST-INSTALL] Restarting Moonraker...\n"
    if [ -x /etc/init.d/S56moonraker_service ]; then
        /etc/init.d/S56moonraker_service restart
    elif command -v systemctl >/dev/null 2>&1; then
        systemctl restart moonraker
    else
        printf "[WARN] Could not restart Moonraker automatically. Please restart it manually.\n"
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
check_klipper_service

KLIPPER_EXTRAS=$(detect_klipper_extras)
printf "[PRE-CHECK] Using Klipper extras: %s\n\n" "$KLIPPER_EXTRAS"

PYTHON=$(detect_python)
printf "[PRE-CHECK] Using Python: %s\n" "$PYTHON"
"$PYTHON" --version
printf "\n"

PIP=$(detect_pip "$PYTHON")
printf "[PRE-CHECK] Using pip: %s\n\n" "$PIP"

download_shaketune
install_python_deps "$PIP"
link_module "$KLIPPER_EXTRAS"
add_updater
restart_klipper
restart_moonraker

printf "\n============================================================\n"
printf "  Shake&Tune K1C installed successfully!\n"
printf "\n"
printf "  Next steps:\n"
printf "  1. Deploy the Docker processing server (see docs/k1c_setup.md)\n"
printf "  2. Add [shaketune] to your printer.cfg with remote_processing_url\n"
printf "  3. Run AXES_SHAPER_CALIBRATION to verify everything works\n"
printf "============================================================\n\n"
