#!/bin/bash

# LPU5 Tactical Tracker - Restart Script for Linux/Unix
# This script gracefully stops and restarts the LPU5 Tactical server

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
echo "  LPU5 TACTICAL TRACKER - RESTART"
echo "========================================"
echo ""

# Function to stop the server
stop_server() {
    echo -e "${BLUE}[*]${NC} Checking for running LPU5 server..."
    
    # Find process using port 8001
    PID=$(lsof -ti:8001 2>/dev/null || echo "")
    
    if [ -z "$PID" ]; then
        echo -e "${YELLOW}[INFO]${NC} No server is currently running on port 8001"
        return 0
    fi
    
    echo -e "${BLUE}[*]${NC} Found server process (PID: $PID)"
    echo -e "${BLUE}[*]${NC} Stopping server gracefully..."
    
    # Try graceful shutdown first (SIGTERM)
    kill -TERM $PID 2>/dev/null || true
    
    # Wait for process to exit (max 10 seconds)
    for i in {1..10}; do
        if ! kill -0 $PID 2>/dev/null; then
            echo -e "${GREEN}[OK]${NC} Server stopped successfully"
            return 0
        fi
        echo -e "${BLUE}[*]${NC} Waiting for server to stop... ($i/10)"
        sleep 1
    done
    
    # Force kill if still running
    if kill -0 $PID 2>/dev/null; then
        echo -e "${YELLOW}[WARN]${NC} Server did not stop gracefully, forcing shutdown..."
        kill -KILL $PID 2>/dev/null || true
        sleep 1
        
        if kill -0 $PID 2>/dev/null; then
            echo -e "${RED}[ERROR]${NC} Failed to stop server"
            return 1
        fi
        echo -e "${GREEN}[OK]${NC} Server forcefully stopped"
    fi
    
    return 0
}

# Function to check if port is available
check_port() {
    if lsof -Pi :8001 -sTCP:LISTEN -t >/dev/null 2>&1; then
        return 1
    fi
    return 0
}

# Stop the server
stop_server

# Wait a moment to ensure port is released
sleep 2

# Verify port is available
if ! check_port; then
    echo -e "${RED}[ERROR]${NC} Port 8001 is still in use after stopping server"
    echo -e "${RED}[ERROR]${NC} Please check for other processes using port 8001:"
    echo -e "${RED}[ERROR]${NC}   lsof -i :8001"
    exit 1
fi

echo ""
echo -e "${GREEN}[*]${NC} Restarting server..."
echo ""

# Start the server using the start script
if [ -f "$SCRIPT_DIR/start_lpu5.sh" ]; then
    exec "$SCRIPT_DIR/start_lpu5.sh"
else
    echo -e "${RED}[ERROR]${NC} start_lpu5.sh not found in $SCRIPT_DIR"
    echo -e "${RED}[ERROR]${NC} Please ensure start_lpu5.sh exists"
    exit 1
fi
