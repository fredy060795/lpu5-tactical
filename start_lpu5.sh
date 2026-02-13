#!/bin/bash

# LPU5 Tactical Tracker - Linux/Unix Startup Script
# This script sets up the environment and starts the LPU5 Tactical server

set -e  # Exit on error

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

# Check if skip update is set
if [ "${SKIP_UPDATE}" = "1" ]; then
    echo -e "${YELLOW}[*]${NC} Auto-update disabled via SKIP_UPDATE=1"
else
    # Create virtual environment if it doesn't exist
    VENV_DIR=".venv"
    if [ ! -d "$VENV_DIR" ]; then
        echo -e "${BLUE}[*]${NC} Creating virtual environment '$VENV_DIR'..."
        python3 -m venv "$VENV_DIR"
        if [ $? -ne 0 ]; then
            echo -e "${RED}[ERROR]${NC} Failed to create virtual environment"
            exit 1
        fi
        echo -e "${GREEN}[OK]${NC} Virtual environment created"
    else
        echo -e "${GREEN}[OK]${NC} Virtual environment exists: $VENV_DIR"
    fi

    # Activate virtual environment
    echo -e "${BLUE}[*]${NC} Activating virtual environment..."
    source "$VENV_DIR/bin/activate"

    # Upgrade pip
    echo -e "${BLUE}[*]${NC} Upgrading pip..."
    pip install --upgrade pip > /dev/null 2>&1

    # Install/update dependencies
    echo -e "${BLUE}[*]${NC} Installing/updating dependencies..."
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt
        if [ $? -ne 0 ]; then
            echo -e "${RED}[ERROR]${NC} Failed to install dependencies"
            exit 1
        fi
    else
        echo -e "${YELLOW}[WARN]${NC} requirements.txt not found"
    fi
fi

# Check for SSL certificates
if [ ! -f "key.pem" ] || [ ! -f "cert.pem" ]; then
    echo -e "${YELLOW}[WARN]${NC} SSL certificates not found (key.pem / cert.pem)"
    echo -e "${YELLOW}[WARN]${NC} Generate self-signed certificates with:"
    echo -e "${YELLOW}[WARN]${NC}   openssl req -x509 -newkey rsa:4096 -nodes -keyout key.pem -out cert.pem -days 365"
    echo ""
    echo -e "${BLUE}[*]${NC} Starting without SSL (HTTP only)..."
    USE_SSL=""
else
    echo -e "${GREEN}[OK]${NC} SSL certificates found"
    USE_SSL="--ssl-keyfile key.pem --ssl-certfile cert.pem"
fi

# Start the server
echo ""
echo -e "${GREEN}[*]${NC} Starting LPU5 Tactical Server..."
echo -e "${BLUE}[*]${NC} Press CTRL+C to stop"
echo ""

# Check if port is already in use
if lsof -Pi :8001 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo -e "${YELLOW}[WARN]${NC} Port 8001 is already in use"
    echo -e "${YELLOW}[WARN]${NC} Use restart_lpu5.sh to restart the server"
    exit 1
fi

# Start uvicorn server
if [ -n "$USE_SSL" ]; then
    python3 -m uvicorn api:app --host 0.0.0.0 --port 8001 $USE_SSL
else
    python3 -m uvicorn api:app --host 0.0.0.0 --port 8001
fi
