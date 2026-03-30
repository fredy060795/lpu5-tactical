#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_nginx_patch.py - Unit tests for nginx_patch.py

Tests the nginx config patching logic used to add a /user-management
proxy location for the OpenTAK Management API.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from nginx_patch import (
    find_nginx_config_files,
    insert_user_mgmt_location,
    patch_nginx_for_user_management,
)


# ---------------------------------------------------------------------------
# Sample nginx configs for testing
# ---------------------------------------------------------------------------

SIMPLE_SERVER_BLOCK = """\
server {
    listen 8446 ssl;
    ssl_certificate /opt/tak/certs/server.crt;

    location / {
        proxy_pass http://127.0.0.1:8080;
    }
}
"""

ALREADY_CONFIGURED = """\
server {
    listen 8446 ssl;

    location /user-management {
        proxy_pass http://127.0.0.1:8081;
    }

    location / {
        proxy_pass http://127.0.0.1:8080;
    }
}
"""

NO_SERVER_BLOCK = """\
http {
    include /etc/nginx/conf.d/*.conf;
}
"""

MULTIPLE_SERVER_BLOCKS = """\
server {
    listen 80;
    return 301 https://$host$request_uri;
}

server {
    listen 8446 ssl;

    location / {
        proxy_pass http://127.0.0.1:8080;
    }
}
"""

SERVER_BRACE_ON_NEXT_LINE = """\
server
{
    listen 8446 ssl;

    location / {
        proxy_pass http://127.0.0.1:8080;
    }
}
"""

NESTED_BLOCKS = """\
server {
    listen 8446 ssl;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        if ($request_method = OPTIONS) {
            return 204;
        }
    }
}
"""

EMPTY_SERVER = """\
server {
    listen 8446 ssl;
}
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInsertUserMgmtLocation(unittest.TestCase):
    """Tests for insert_user_mgmt_location()."""

    def test_simple_server_block(self):
        """Insert into a typical reverse-proxy server block."""
        result = insert_user_mgmt_location(SIMPLE_SERVER_BLOCK)
        self.assertIsNotNone(result)
        self.assertIn("location /user-management {", result)
        self.assertIn("proxy_pass http://127.0.0.1:8081;", result)
        # Original content should still be present.
        self.assertIn("location / {", result)
        self.assertIn("proxy_pass http://127.0.0.1:8080;", result)
        # The block should be inside the server block (before the last }).
        lines = result.strip().split("\n")
        self.assertEqual(lines[-1].strip(), "}")

    def test_already_configured_returns_none(self):
        """Return None when /user-management is already present."""
        result = insert_user_mgmt_location(ALREADY_CONFIGURED)
        self.assertIsNone(result)

    def test_no_server_block_returns_none(self):
        """Return None when there is no server block."""
        result = insert_user_mgmt_location(NO_SERVER_BLOCK)
        self.assertIsNone(result)

    def test_multiple_server_blocks_patches_last(self):
        """Insert into the last server block when multiple exist."""
        result = insert_user_mgmt_location(MULTIPLE_SERVER_BLOCKS)
        self.assertIsNotNone(result)
        self.assertIn("location /user-management {", result)
        # The first server block (port 80 redirect) should be unchanged.
        self.assertIn("return 301", result)
        # Verify inserted in the second block (after proxy_pass 8080).
        idx_8080 = result.index("proxy_pass http://127.0.0.1:8080")
        idx_mgmt = result.index("location /user-management")
        self.assertGreater(idx_mgmt, idx_8080)

    def test_server_brace_on_next_line(self):
        """Handle configs where the opening brace is on the next line."""
        result = insert_user_mgmt_location(SERVER_BRACE_ON_NEXT_LINE)
        self.assertIsNotNone(result)
        self.assertIn("location /user-management {", result)

    def test_nested_blocks(self):
        """Correctly handle nested blocks (e.g. if {} inside location)."""
        result = insert_user_mgmt_location(NESTED_BLOCKS)
        self.assertIsNotNone(result)
        self.assertIn("location /user-management {", result)
        # The existing nested 'if' block should still be intact.
        self.assertIn("if ($request_method = OPTIONS)", result)

    def test_custom_backend_port(self):
        """Use a non-default backend port."""
        result = insert_user_mgmt_location(SIMPLE_SERVER_BLOCK, backend_port=9090)
        self.assertIsNotNone(result)
        self.assertIn("proxy_pass http://127.0.0.1:9090;", result)
        self.assertNotIn("8081", result)

    def test_empty_server_block(self):
        """Insert into a server block with no existing location."""
        result = insert_user_mgmt_location(EMPTY_SERVER)
        self.assertIsNotNone(result)
        self.assertIn("location /user-management {", result)

    def test_preserves_original_structure(self):
        """The patched config should still end with a closing brace."""
        result = insert_user_mgmt_location(SIMPLE_SERVER_BLOCK)
        self.assertIsNotNone(result)
        # Count braces: should still be balanced.
        opens = result.count("{")
        closes = result.count("}")
        self.assertEqual(opens, closes)

    def test_idempotent(self):
        """Applying the patch twice should not duplicate the block."""
        first = insert_user_mgmt_location(SIMPLE_SERVER_BLOCK)
        self.assertIsNotNone(first)
        second = insert_user_mgmt_location(first)
        self.assertIsNone(second, "Second application should return None (already present)")

    def test_comment_with_server_keyword_ignored(self):
        """A comment containing 'server' should not be mistaken for a server block."""
        config = """\
# This is not a server block
# server { listen 80; }
http {
    include mime.types;
}
"""
        result = insert_user_mgmt_location(config)
        self.assertIsNone(result)


class TestPatchNginxForUserManagement(unittest.TestCase):
    """Integration tests for patch_nginx_for_user_management() using temp dirs."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Monkey-patch the search dirs AND main conf to use our temp directory.
        import nginx_patch
        self._orig_search_dirs = nginx_patch._NGINX_SEARCH_DIRS[:]
        self._orig_main_conf = nginx_patch._NGINX_MAIN_CONF
        nginx_patch._NGINX_SEARCH_DIRS = [self.tmpdir]
        nginx_patch._NGINX_MAIN_CONF = os.path.join(self.tmpdir, "nginx.conf")

    def tearDown(self):
        import nginx_patch
        nginx_patch._NGINX_SEARCH_DIRS = self._orig_search_dirs
        nginx_patch._NGINX_MAIN_CONF = self._orig_main_conf
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_config(self, name: str, content: str) -> str:
        path = os.path.join(self.tmpdir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_no_configs_found(self):
        """Return failure when no config files exist."""
        # tmpdir is empty
        result = patch_nginx_for_user_management()
        self.assertFalse(result["success"])
        self.assertIn("No nginx configuration files found", result["error"])

    def test_already_configured(self):
        """Return success with already_configured flag."""
        self._write_config("opentak.conf", ALREADY_CONFIGURED)
        result = patch_nginx_for_user_management()
        self.assertTrue(result["success"])
        self.assertTrue(result.get("already_configured"))

    def test_patches_config_file(self):
        """Successfully patch a config and verify contents."""
        path = self._write_config("opentak.conf", SIMPLE_SERVER_BLOCK)
        # Mock subprocess.run so nginx -t and reload don't run on the CI host.
        mock_result = MagicMock(returncode=0, stderr="", stdout="")
        with patch("nginx_patch.subprocess.run", return_value=mock_result):
            result = patch_nginx_for_user_management()
        self.assertTrue(result.get("success"), result)
        # Verify the file was modified.
        with open(path) as f:
            new_content = f.read()
        self.assertIn("location /user-management {", new_content)
        self.assertIn("proxy_pass http://127.0.0.1:8081;", new_content)
        # Verify backup was created.
        self.assertTrue(os.path.isfile(path + ".lpu5.bak"))

    def test_prefers_proxy_pass_config(self):
        """When multiple configs exist, prefer the one with proxy_pass."""
        self._write_config("plain.conf", EMPTY_SERVER)
        proxy_path = self._write_config("proxy.conf", SIMPLE_SERVER_BLOCK)
        mock_result = MagicMock(returncode=0, stderr="", stdout="")
        with patch("nginx_patch.subprocess.run", return_value=mock_result):
            result = patch_nginx_for_user_management()
        self.assertTrue(result.get("success"), result)
        self.assertEqual(result["path"], proxy_path)

    def test_no_server_block(self):
        """Return failure when config has no server block."""
        self._write_config("http-only.conf", NO_SERVER_BLOCK)
        result = patch_nginx_for_user_management()
        self.assertFalse(result["success"])


class TestFindNginxConfigFiles(unittest.TestCase):
    """Tests for find_nginx_config_files() using temp dirs."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import nginx_patch
        self._orig_search_dirs = nginx_patch._NGINX_SEARCH_DIRS[:]
        self._orig_main_conf = nginx_patch._NGINX_MAIN_CONF
        nginx_patch._NGINX_SEARCH_DIRS = [self.tmpdir]
        nginx_patch._NGINX_MAIN_CONF = os.path.join(self.tmpdir, "nginx.conf")

    def tearDown(self):
        import nginx_patch
        nginx_patch._NGINX_SEARCH_DIRS = self._orig_search_dirs
        nginx_patch._NGINX_MAIN_CONF = self._orig_main_conf
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_finds_files_in_search_dir(self):
        """Config files in search directories are found."""
        path = os.path.join(self.tmpdir, "test.conf")
        with open(path, "w") as f:
            f.write("server {}")
        result = find_nginx_config_files()
        self.assertIn(path, result)

    def test_empty_search_dir(self):
        """Empty search directories return empty list."""
        result = find_nginx_config_files()
        # May include /etc/nginx/nginx.conf if it exists on the system
        # but our temp dir has no files.
        self.assertNotIn(os.path.join(self.tmpdir, "anything"), result)


if __name__ == "__main__":
    unittest.main()
