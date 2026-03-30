#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nginx_patch.py - Helpers for patching nginx configs to proxy /user-management.

When OpenTAK Server runs behind nginx the ``/user-management`` path must be
proxied to the management backend (default port 8081).  This module provides
functions that:

1. Search common locations for nginx configuration files.
2. Parse server blocks and detect whether the proxy is already present.
3. Insert a ``location /user-management`` block into the config.
4. Validate with ``nginx -t`` and reload nginx.
"""

import os
import pathlib
import re
import shutil
import subprocess
from typing import Dict, List, Optional

# Directories where nginx configs are commonly found.
_NGINX_SEARCH_DIRS: List[str] = [
    "/etc/nginx/conf.d",
    "/etc/nginx/sites-enabled",
    "/etc/nginx/sites-available",
    "/opt/tak/nginx",
]

# Main nginx config path (also checked in find_nginx_config_files).
_NGINX_MAIN_CONF: str = "/etc/nginx/nginx.conf"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def find_nginx_config_files() -> List[str]:
    """Return a list of nginx config file paths found in common locations."""
    found: List[str] = []
    main = _NGINX_MAIN_CONF
    if os.path.isfile(main):
        found.append(main)
    for search_dir in _NGINX_SEARCH_DIRS:
        if not os.path.isdir(search_dir):
            continue
        for name in sorted(os.listdir(search_dir)):
            full = os.path.join(search_dir, name)
            if os.path.isfile(full):
                found.append(full)
    return found


def insert_user_mgmt_location(config_text: str, backend_port: int = 8081) -> Optional[str]:
    """Insert a ``/user-management`` location block into *config_text*.

    The block is placed just before the closing ``}`` of the **last**
    ``server`` block found in the text.

    Returns the modified text, or ``None`` when:
    * the block already exists, or
    * no suitable ``server`` block is found.
    """
    if re.search(r"location\s+/user-management\b", config_text):
        return None  # already present

    location_block = (
        "\n"
        "    # Proxy OpenTAK user-management API (added by LPU5 Tactical)\n"
        "    location /user-management {\n"
        f"        proxy_pass http://127.0.0.1:{backend_port};\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Forwarded-For $remote_addr;\n"
        "    }\n"
    )

    lines = config_text.split("\n")

    # Locate ``server`` block starts.
    server_starts: List[int] = []
    for i, line in enumerate(lines):
        stripped = line.split("#")[0]  # ignore comments
        if re.search(r"\bserver\s*\{", stripped) or re.search(
            r"\bserver\s*$", stripped.strip()
        ):
            server_starts.append(i)

    if not server_starts:
        return None

    # Walk from the last ``server`` start and track brace depth.
    start = server_starts[-1]
    depth = 0
    started = False
    end_idx: Optional[int] = None
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth > 0:
            started = True
        if started and depth <= 0:
            end_idx = i
            break

    if end_idx is None:
        return None

    # Insert location block lines just before the closing ``}``.
    block_lines = location_block.rstrip("\n").split("\n")
    new_lines = lines[:end_idx] + block_lines + lines[end_idx:]
    return "\n".join(new_lines)


def patch_nginx_for_user_management(backend_port: int = 8081) -> Dict:
    """Add a ``/user-management`` proxy location to the local nginx config.

    High-level workflow:
    1. Search common paths for nginx config files.
    2. Pick a file that contains a ``server`` block (prefer one that
       already has ``proxy_pass``, i.e. a reverse-proxy config).
    3. Insert the location block.
    4. Validate the new config with ``nginx -t``.
    5. Reload nginx.

    Returns a dict describing the outcome.
    """
    configs = find_nginx_config_files()
    if not configs:
        return {
            "success": False,
            "error": (
                "No nginx configuration files found.  "
                "Searched /etc/nginx/nginx.conf, /etc/nginx/conf.d/, "
                "/etc/nginx/sites-enabled/, /opt/tak/nginx/"
            ),
        }

    # Check whether already configured.
    for path in configs:
        try:
            text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError):
            continue
        if re.search(r"location\s+/user-management\b", text):
            return {
                "success": True,
                "already_configured": True,
                "message": f"/user-management proxy already present in {path}",
                "path": path,
            }

    # Find the best file to patch.
    target_path: Optional[str] = None
    target_text: Optional[str] = None
    fallback_path: Optional[str] = None
    fallback_text: Optional[str] = None

    for path in configs:
        try:
            text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError):
            continue
        if "server" not in text:
            continue
        # Prefer files that also have proxy_pass (reverse-proxy configs).
        if "proxy_pass" in text:
            target_path, target_text = path, text
            break
        if fallback_path is None:
            fallback_path, fallback_text = path, text

    if target_path is None:
        target_path, target_text = fallback_path, fallback_text

    if target_path is None or target_text is None:
        return {
            "success": False,
            "error": "No nginx config with a server block found.",
            "searched": configs,
        }

    patched = insert_user_mgmt_location(target_text, backend_port)
    if patched is None:
        return {
            "success": False,
            "error": f"Could not find a suitable insertion point in {target_path}",
        }

    # Write patched config (with backup).
    backup_path = target_path + ".lpu5.bak"
    try:
        shutil.copy2(target_path, backup_path)
        pathlib.Path(target_path).write_text(patched, encoding="utf-8")
    except PermissionError:
        return {
            "success": False,
            "error": (
                f"Permission denied writing to {target_path}.  "
                "Run the server as root or manually add the "
                "/user-management location block."
            ),
        }
    except OSError as exc:
        return {"success": False, "error": f"Failed to write {target_path}: {exc}"}

    # Validate with ``nginx -t``.
    try:
        result = subprocess.run(
            ["nginx", "-t"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            shutil.copy2(backup_path, target_path)
            return {
                "success": False,
                "error": (
                    "nginx config validation failed – restored backup.  "
                    f"nginx -t output: {result.stderr.strip()}"
                ),
            }
    except FileNotFoundError:
        pass  # nginx binary not found – skip validation
    except Exception as exc:
        try:
            shutil.copy2(backup_path, target_path)
        except OSError:
            pass
        return {"success": False, "error": f"Error running nginx -t: {exc}"}

    # Reload nginx.
    reloaded = False
    for cmd in (["nginx", "-s", "reload"], ["systemctl", "reload", "nginx"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                reloaded = True
                break
        except (FileNotFoundError, Exception):
            continue

    msg = f"Added /user-management proxy to {target_path}"
    if reloaded:
        msg += " and reloaded nginx"
    else:
        msg += " (nginx reload skipped – please reload manually)"

    return {
        "success": True,
        "message": msg,
        "path": target_path,
        "backup": backup_path,
    }
