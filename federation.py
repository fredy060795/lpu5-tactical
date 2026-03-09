#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
federation.py – LPU5 Server Federation Core

Provides cryptographic primitives and helpers for the LPU5 server federation
system, inspired by ATAK's server trust model:

  1. Each LPU5 server owns an RSA-2048 key pair (server_private.pem /
     server_public.pem).  The pair is generated automatically on first boot.
  2. Servers exchange public keys via QR code (scan-to-register).
  3. A mutual challenge/response handshake (PKCS#1 v1.5 + SHA-256) establishes
     trust before any data is synchronised.
  4. Only *trusted* peers participate in data exchange (CoT, markers, etc.).

Key functions:
    load_or_generate_server_keypair(base_path)  → (private_key, public_key)
    get_server_info(base_path)                  → dict  (ID, name, public key …)
    sign_challenge(challenge_b64, private_key)  → signature_b64
    verify_signature(challenge_b64, sig_b64, public_key_pem)  → bool
    compute_fingerprint(public_key)             → hex str
    make_server_info_qr_png(info_dict)          → bytes  (PNG)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any

logger = logging.getLogger("lpu5-federation")

# ---------------------------------------------------------------------------
# Lazy-import cryptography (already in requirements.txt)
# ---------------------------------------------------------------------------
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.backends import default_backend
    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CRYPTO_AVAILABLE = False
    logger.warning("cryptography library not available – federation disabled")

# ---------------------------------------------------------------------------
# QR code generation (already in requirements.txt)
# ---------------------------------------------------------------------------
try:
    import qrcode as _qrcode
    from io import BytesIO as _BytesIO
    from PIL import Image as _Image
    _QR_AVAILABLE = True
except ImportError:  # pragma: no cover
    _qrcode = None  # type: ignore
    _QR_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PRIVATE_KEY_FILE = "server_private.pem"
_PUBLIC_KEY_FILE = "server_public.pem"
_SERVER_ID_FILE = "server_id.txt"
_KEY_SIZE = 2048
_CHALLENGE_EXPIRE_SECONDS = 120   # 2-minute window for challenge response

# ---------------------------------------------------------------------------
# Key-pair management
# ---------------------------------------------------------------------------

def load_or_generate_server_keypair(base_path: str) -> Tuple[Any, Any]:
    """
    Load the server RSA key pair from *base_path*, generating it on first call.

    Returns
    -------
    (private_key, public_key)  –  cryptography RSAPrivateKey / RSAPublicKey
    """
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography library is required for federation")

    priv_path = os.path.join(base_path, _PRIVATE_KEY_FILE)
    pub_path = os.path.join(base_path, _PUBLIC_KEY_FILE)

    if os.path.exists(priv_path) and os.path.exists(pub_path):
        with open(priv_path, "rb") as fh:
            private_key = serialization.load_pem_private_key(
                fh.read(), password=None, backend=default_backend()
            )
        with open(pub_path, "rb") as fh:
            public_key = serialization.load_pem_public_key(
                fh.read(), backend=default_backend()
            )
        logger.info("Loaded existing server key pair from %s", base_path)
        return private_key, public_key

    # Generate new RSA-2048 key pair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=_KEY_SIZE,
        backend=default_backend(),
    )
    public_key = private_key.public_key()

    # Persist to disk
    with open(priv_path, "wb") as fh:
        fh.write(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    with open(pub_path, "wb") as fh:
        fh.write(
            public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
    logger.info("Generated new RSA-%d server key pair in %s", _KEY_SIZE, base_path)
    return private_key, public_key


def get_public_key_pem(base_path: str) -> str:
    """Return the local server's public key as a PEM string."""
    _, public_key = load_or_generate_server_keypair(base_path)
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def get_or_create_server_id(base_path: str) -> str:
    """Return a stable UUID that identifies this server instance."""
    id_path = os.path.join(base_path, _SERVER_ID_FILE)
    if os.path.exists(id_path):
        with open(id_path) as fh:
            return fh.read().strip()
    server_id = str(uuid.uuid4())
    with open(id_path, "w") as fh:
        fh.write(server_id)
    return server_id


def compute_fingerprint(public_key: Any) -> str:
    """
    Return the SHA-256 fingerprint (hex) of the DER-encoded public key.
    This acts as a human-verifiable short identifier for a key.
    """
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def compute_fingerprint_from_pem(public_key_pem: str) -> str:
    """Compute SHA-256 fingerprint from a PEM string."""
    pub_key = serialization.load_pem_public_key(
        public_key_pem.encode("utf-8"), backend=default_backend()
    )
    return compute_fingerprint(pub_key)


# ---------------------------------------------------------------------------
# Server info (payload for QR code / REST)
# ---------------------------------------------------------------------------

def get_server_info(base_path: str, name: Optional[str] = None, url: Optional[str] = None) -> Dict[str, Any]:
    """
    Return a dict describing this server.  This is what gets encoded in the
    QR code and exchanged during onboarding.

    Fields
    ------
    server_id   – stable UUID for this instance
    name        – human-readable server name (defaults to hostname)
    url         – optional base URL (e.g. https://192.168.1.10:8101)
    public_key  – RSA public key in PEM format
    fingerprint – SHA-256 fingerprint of the public key (hex)
    timestamp   – ISO-8601 UTC timestamp of when this info was generated
    """
    private_key, public_key = load_or_generate_server_keypair(base_path)
    server_id = get_or_create_server_id(base_path)
    hostname = name or socket.gethostname()

    return {
        "server_id": server_id,
        "name": hostname,
        "url": url or "",
        "public_key": public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8"),
        "fingerprint": compute_fingerprint(public_key),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Challenge / response (PKCS#1 v1.5, SHA-256)
# ---------------------------------------------------------------------------

def generate_challenge() -> str:
    """Generate 32 random bytes, return as base64."""
    raw = os.urandom(32)
    return base64.b64encode(raw).decode("ascii")


def sign_challenge(challenge_b64: str, private_key: Any) -> str:
    """
    Sign a base64-encoded challenge with the server's private key.

    Returns base64-encoded signature string.
    """
    challenge_bytes = base64.b64decode(challenge_b64)
    signature = private_key.sign(
        challenge_bytes,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def verify_signature(challenge_b64: str, signature_b64: str, public_key_pem: str) -> bool:
    """
    Verify a challenge signature using the peer's public key.

    Returns True if valid, False otherwise (never raises on bad signatures).
    """
    if not _CRYPTO_AVAILABLE:
        return False
    try:
        pub_key = serialization.load_pem_public_key(
            public_key_pem.encode("utf-8"), backend=default_backend()
        )
        challenge_bytes = base64.b64decode(challenge_b64)
        signature = base64.b64decode(signature_b64)
        pub_key.verify(
            signature,
            challenge_bytes,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# QR code helpers
# ---------------------------------------------------------------------------

def make_server_info_qr_png(info_dict: Dict[str, Any]) -> bytes:
    """
    Encode *info_dict* (JSON) into a QR code and return a PNG byte string.
    Raises RuntimeError if qrcode / Pillow is not available.
    """
    if not _QR_AVAILABLE:
        raise RuntimeError("qrcode / Pillow libraries are required for QR generation")

    payload = json.dumps(info_dict, separators=(",", ":"))

    qr = _qrcode.QRCode(
        version=None,
        error_correction=_qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = _BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
