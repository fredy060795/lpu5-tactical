#!/bin/bash

# LPU5 Tactical Tracker - Linux/Unix Startup Script
# This script sets up the environment and starts the LPU5 Tactical server

# NOTE: We intentionally do NOT use 'set -e' here.
# pip install may fail for optional packages (e.g. pyrtlsdr, numpy)
# and that must not abort the whole startup.

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo ""
echo "========================================"
echo "  LPU5 TACTICAL TRACKER"
echo "  Start (with automatic dependency update)"
echo "========================================"
echo ""

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}[ERROR]${NC} Python 3 is not installed. Please install Python 3.8 or higher."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo -e "${BLUE}[INFO]${NC} Python version: $PYTHON_VERSION"

# Check for minimum Python version (3.8)
if ! python3 -c 'import sys; exit(0 if sys.version_info >= (3,8) else 1)'; then
    echo -e "${RED}[ERROR]${NC} Python 3.8 or higher is required. Current version: $PYTHON_VERSION"
    exit 1
fi

# ── Virtual environment setup ──────────────────────────────────────────────
VENV_DIR=".venv"

# Create virtual environment if it doesn't exist (always, regardless of SKIP_UPDATE)
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${BLUE}[*]${NC} Creating virtual environment '$VENV_DIR'..."
    python3 -m venv "$VENV_DIR"
    if [ $? -ne 0 ]; then
        echo -e "${RED}[ERROR]${NC} Failed to create virtual environment."
        echo -e "${RED}[ERROR]${NC} On Debian/Ubuntu you may need: sudo apt install python3-venv"
        exit 1
    fi
    echo -e "${GREEN}[OK]${NC} Virtual environment created"
else
    echo -e "${GREEN}[OK]${NC} Virtual environment exists: $VENV_DIR"
fi

# Activate virtual environment (always – ensures the correct Python & packages are used)
if [ -f "$VENV_DIR/bin/activate" ]; then
    echo -e "${BLUE}[*]${NC} Activating virtual environment..."
    source "$VENV_DIR/bin/activate"
else
    echo -e "${RED}[ERROR]${NC} Virtual environment is broken (no bin/activate found)."
    echo -e "${RED}[ERROR]${NC} Delete '$VENV_DIR' and re-run this script."
    exit 1
fi

# ── Dependency installation ────────────────────────────────────────────────
if [ "${SKIP_UPDATE}" = "1" ]; then
    echo -e "${YELLOW}[*]${NC} Dependency update disabled via SKIP_UPDATE=1"
else
    # Upgrade pip
    echo -e "${BLUE}[*]${NC} Upgrading pip..."
    pip install --upgrade pip > /dev/null 2>&1

    # Install/update core dependencies
    echo -e "${BLUE}[*]${NC} Installing/updating core dependencies..."
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt
        if [ $? -ne 0 ]; then
            echo -e "${RED}[ERROR]${NC} Failed to install core dependencies"
            exit 1
        fi
        echo -e "${GREEN}[OK]${NC} Core dependencies installed"
    else
        echo -e "${YELLOW}[WARN]${NC} requirements.txt not found"
    fi

    # Install optional SDR dependencies (failure is non-fatal)
    echo -e "${BLUE}[*]${NC} Installing optional SDR dependencies (pyrtlsdr, numpy)..."
    if pip install "pyrtlsdr>=0.3.0" "numpy>=1.24.0"; then
        echo -e "${GREEN}[OK]${NC} Optional SDR dependencies installed"
    else
        echo -e "${YELLOW}[WARN]${NC} Optional SDR packages could not be installed."
        echo -e "${YELLOW}[WARN]${NC} SDR features will not be available. The server will still start."
        echo -e "${YELLOW}[WARN]${NC} To install manually later: pip install pyrtlsdr numpy"
    fi
fi

# ── Hardware dependency checks ─────────────────────────────────────────────
echo ""
echo -e "${BLUE}[*]${NC} Checking hardware dependencies..."

SDR_TOOLS_MISSING=0

for tool in rtl_tcp rtl_power rtl_test rtl_fm; do
    if command -v "$tool" &> /dev/null; then
        echo -e "${GREEN}[OK]${NC} $tool found: $(command -v $tool)"
    else
        echo -e "${YELLOW}[WARN]${NC} $tool not found"
        SDR_TOOLS_MISSING=1
    fi
done

if [ "$SDR_TOOLS_MISSING" = "1" ]; then
    echo ""
    echo -e "${YELLOW}[WARN]${NC} One or more RTL-SDR system tools are missing."
    echo -e "${YELLOW}[WARN]${NC} SDR features (spectrum view, audio streaming) will not be available"
    echo -e "${YELLOW}[WARN]${NC} until these tools are installed."
    echo ""
    echo -e "${BLUE}[INFO]${NC} Install RTL-SDR tools:"
    echo -e "${BLUE}[INFO]${NC}   Debian/Ubuntu/Raspberry Pi:  sudo apt install rtl-sdr"
    echo -e "${BLUE}[INFO]${NC}   Fedora/RHEL:                 sudo dnf install rtl-sdr"
    echo -e "${BLUE}[INFO]${NC}   Arch Linux:                  sudo pacman -S rtl-sdr"
    echo -e "${BLUE}[INFO]${NC} After installing, start rtl_tcp with: rtl_tcp -a 0.0.0.0"
    echo ""
    echo -e "${BLUE}[INFO]${NC} You can also check dependency status at runtime via:"
    echo -e "${BLUE}[INFO]${NC}   GET /api/dependencies/check"
    echo ""
fi
# ── End hardware dependency checks ────────────────────────────────────────

# Start the server
# Using 'python3 api.py' instead of 'python3 -m uvicorn ...' so that the
# __main__ block runs.  This auto-generates SSL certificates when missing,
# prints the startup banner with access URLs, and handles all uvicorn
# configuration (host, port, SSL, timeouts) in one place.
echo ""
echo -e "${GREEN}[*]${NC} Starting LPU5 Tactical Server..."
echo -e "${BLUE}[*]${NC} Press CTRL+C to stop"
echo ""

# Check if port is already in use
if command -v lsof &> /dev/null; then
    if lsof -Pi :8101 -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo -e "${YELLOW}[WARN]${NC} Port 8101 is already in use"
        echo -e "${YELLOW}[WARN]${NC} Use restart_lpu5.sh to restart the server"
        exit 1
    fi
elif command -v ss &> /dev/null; then
    if ss -tlnp 2>/dev/null | grep -q ':8101 '; then
        echo -e "${YELLOW}[WARN]${NC} Port 8101 is already in use"
        echo -e "${YELLOW}[WARN]${NC} Use restart_lpu5.sh to restart the server"
        exit 1
    fi
fi

python3 api.py
