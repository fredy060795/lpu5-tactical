#!/bin/bash
# setup_nginx_tak_proxy.sh - Add /user-management proxy block to nginx config
#
# When OpenTAK Server runs behind nginx, the /user-management path must be
# proxied to the management backend (default port 8081).  This script
# automatically finds the nginx config, adds the location block, validates,
# and reloads nginx.
#
# Usage:
#   sudo bash setup_nginx_tak_proxy.sh
#   sudo bash setup_nginx_tak_proxy.sh [backend_port]   # default: 8081

set -e

BACKEND_PORT="${1:-8081}"

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo ""
echo "========================================"
echo "  LPU5 - Nginx TAK Proxy Setup"
echo "========================================"
echo ""

# ── Preflight checks ──────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}[ERROR]${NC} This script must be run as root (use sudo)"
    exit 1
fi

if ! command -v nginx &> /dev/null; then
    echo -e "${RED}[ERROR]${NC} nginx is not installed"
    exit 1
fi

# ── Locate nginx config files ─────────────────────────────────────────────
SEARCH_DIRS="/etc/nginx/conf.d /etc/nginx/sites-enabled /etc/nginx/sites-available /opt/tak/nginx"
CONFIGS=()

if [ -f /etc/nginx/nginx.conf ]; then
    CONFIGS+=("/etc/nginx/nginx.conf")
fi

for dir in $SEARCH_DIRS; do
    if [ -d "$dir" ]; then
        for f in "$dir"/*; do
            [ -f "$f" ] && CONFIGS+=("$f")
        done
    fi
done

if [ ${#CONFIGS[@]} -eq 0 ]; then
    echo -e "${RED}[ERROR]${NC} No nginx config files found"
    exit 1
fi

echo -e "${BLUE}[INFO]${NC} Found ${#CONFIGS[@]} config file(s)"

# ── Check if already configured ───────────────────────────────────────────
for conf in "${CONFIGS[@]}"; do
    if grep -qE 'location\s+/user-management\b' "$conf" 2>/dev/null; then
        echo -e "${GREEN}[OK]${NC} /user-management proxy already configured in $conf"
        exit 0
    fi
done

# ── Find best config to patch ─────────────────────────────────────────────
TARGET=""
FALLBACK=""

for conf in "${CONFIGS[@]}"; do
    if grep -q 'server' "$conf" && grep -q 'proxy_pass' "$conf"; then
        TARGET="$conf"
        break
    fi
    if grep -q 'server' "$conf" && [ -z "$FALLBACK" ]; then
        FALLBACK="$conf"
    fi
done

[ -z "$TARGET" ] && TARGET="$FALLBACK"

if [ -z "$TARGET" ]; then
    echo -e "${RED}[ERROR]${NC} No suitable nginx server block found"
    exit 1
fi

echo -e "${BLUE}[INFO]${NC} Patching: $TARGET"

# ── Backup ─────────────────────────────────────────────────────────────────
BACKUP="${TARGET}.lpu5.bak"
cp -p "$TARGET" "$BACKUP"
echo -e "${BLUE}[INFO]${NC} Backup created: $BACKUP"

# ── Patch the config using Python (reliable brace tracking) ───────────────
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PYTHON=python3
if [ -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python3"
fi

$PYTHON - "$TARGET" "$BACKEND_PORT" << 'PYEOF'
import re, sys, pathlib

target = sys.argv[1]
port = int(sys.argv[2])

text = pathlib.Path(target).read_text(encoding="utf-8", errors="replace")

# Abort if already present
if re.search(r"location\s+/user-management\b", text):
    print("Already configured", file=sys.stderr)
    sys.exit(0)

lines = text.split("\n")

# Find the last 'server' block start
server_starts = []
for i, line in enumerate(lines):
    stripped = line.split("#")[0]
    if re.search(r"\bserver\s*\{", stripped) or re.search(r"\bserver\s*$", stripped.strip()):
        server_starts.append(i)

if not server_starts:
    print("ERROR: No server block found", file=sys.stderr)
    sys.exit(1)

start = server_starts[-1]
depth = 0
started = False
end_idx = None
for i in range(start, len(lines)):
    depth += lines[i].count("{") - lines[i].count("}")
    if depth > 0:
        started = True
    if started and depth <= 0:
        end_idx = i
        break

if end_idx is None:
    print("ERROR: Could not find server block end", file=sys.stderr)
    sys.exit(1)

block = [
    "",
    "    # Proxy OpenTAK user-management API (added by LPU5 Tactical)",
    "    location /user-management {",
    f"        proxy_pass http://127.0.0.1:{port};",
    "        proxy_http_version 1.1;",
    "        proxy_set_header Host $host;",
    "        proxy_set_header X-Forwarded-For $remote_addr;",
    "    }",
]

new_lines = lines[:end_idx] + block + lines[end_idx:]
pathlib.Path(target).write_text("\n".join(new_lines), encoding="utf-8")
print(f"Inserted /user-management location before line {end_idx + 1}")
PYEOF

if [ $? -ne 0 ]; then
    echo -e "${RED}[ERROR]${NC} Failed to patch config – restoring backup"
    cp -p "$BACKUP" "$TARGET"
    exit 1
fi

echo -e "${GREEN}[OK]${NC} Location block added"

# ── Validate ───────────────────────────────────────────────────────────────
echo -e "${BLUE}[INFO]${NC} Validating nginx config..."
if ! nginx -t 2>&1; then
    echo -e "${RED}[ERROR]${NC} Validation failed – restoring backup"
    cp -p "$BACKUP" "$TARGET"
    exit 1
fi
echo -e "${GREEN}[OK]${NC} Config valid"

# ── Reload ─────────────────────────────────────────────────────────────────
echo -e "${BLUE}[INFO]${NC} Reloading nginx..."
if nginx -s reload 2>/dev/null || systemctl reload nginx 2>/dev/null; then
    echo -e "${GREEN}[OK]${NC} Nginx reloaded successfully"
else
    echo -e "${YELLOW}[WARN]${NC} Could not reload nginx automatically – please reload manually"
fi

echo ""
echo -e "${GREEN}[DONE]${NC} /user-management proxy block added to $TARGET"
echo ""
