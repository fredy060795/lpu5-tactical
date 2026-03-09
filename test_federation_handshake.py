#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_federation_handshake.py – Unit tests for the automatic federation handshake.

Exercises the federation.py crypto helpers used in the new
/api/federation/handshake/init and /api/federation/handshake/complete flow.
"""

import os
import shutil
import tempfile
import unittest

from federation import (
    load_or_generate_server_keypair,
    get_server_info,
    generate_challenge,
    sign_challenge,
    verify_signature,
    compute_fingerprint_from_pem,
)


class TestFederationHandshakeFlow(unittest.TestCase):
    """
    Simulates the full automatic mutual handshake between two servers
    (Server A and Server B) using only the federation.py primitives.
    """

    def setUp(self):
        """Create two temporary directories to act as two separate servers."""
        self.dir_a = tempfile.mkdtemp(prefix="fed_server_a_")
        self.dir_b = tempfile.mkdtemp(prefix="fed_server_b_")

    def tearDown(self):
        shutil.rmtree(self.dir_a, ignore_errors=True)
        shutil.rmtree(self.dir_b, ignore_errors=True)

    def test_mutual_handshake(self):
        """Full round-trip: init → verify → complete → mutual trust."""

        # ── Server A generates its key pair and info ─────────────────
        priv_a, pub_a = load_or_generate_server_keypair(self.dir_a)
        info_a = get_server_info(self.dir_a, name="Server-A", url="https://a.local:8101")

        # ── Server B generates its key pair and info ─────────────────
        priv_b, pub_b = load_or_generate_server_keypair(self.dir_b)
        info_b = get_server_info(self.dir_b, name="Server-B", url="https://b.local:8101")

        # ── Phase 1: Server A sends init request to Server B ─────────
        challenge_from_a = generate_challenge()

        # Server B processes the init request:
        # 1. Validate A's public key
        fp_a = compute_fingerprint_from_pem(info_a["public_key"])
        self.assertEqual(fp_a, info_a["fingerprint"])

        # 2. Sign A's challenge with B's private key
        sig_b = sign_challenge(challenge_from_a, priv_b)

        # 3. Create counter-challenge for A
        counter_challenge = generate_challenge()

        # Server B returns: info_b, sig_b, counter_challenge

        # ── Server A verifies B's signature ──────────────────────────
        valid = verify_signature(challenge_from_a, sig_b, info_b["public_key"])
        self.assertTrue(valid, "Server A should verify Server B's signature")

        # ── Phase 2: Server A signs counter-challenge and sends complete
        counter_sig_a = sign_challenge(counter_challenge, priv_a)

        # ── Server B verifies A's counter-signature ──────────────────
        valid2 = verify_signature(counter_challenge, counter_sig_a, info_a["public_key"])
        self.assertTrue(valid2, "Server B should verify Server A's counter-signature")

    def test_wrong_key_fails(self):
        """Signature from a different key must fail verification."""
        priv_a, _ = load_or_generate_server_keypair(self.dir_a)
        _, pub_b = load_or_generate_server_keypair(self.dir_b)
        info_b = get_server_info(self.dir_b)

        challenge = generate_challenge()
        sig_a = sign_challenge(challenge, priv_a)

        # Verify A's signature against B's key → must fail
        valid = verify_signature(challenge, sig_a, info_b["public_key"])
        self.assertFalse(valid, "Signature from wrong key should be rejected")

    def test_tampered_challenge_fails(self):
        """Modified challenge must fail verification."""
        priv_a, _ = load_or_generate_server_keypair(self.dir_a)
        info_a = get_server_info(self.dir_a)

        challenge = generate_challenge()
        sig = sign_challenge(challenge, priv_a)

        # Tamper with challenge
        different_challenge = generate_challenge()
        self.assertNotEqual(challenge, different_challenge)
        valid = verify_signature(different_challenge, sig, info_a["public_key"])
        self.assertFalse(valid, "Tampered challenge should fail verification")


if __name__ == "__main__":
    unittest.main()
