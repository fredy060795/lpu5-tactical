#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_cert.py - Self-Signed SSL Certificate Generator for LPU5 Tactical Tracker

Generates a self-signed TLS certificate and private key so the API can
start with HTTPS enabled by default.  Called automatically from api.py
when cert.pem / key.pem are not found.
"""

import datetime
import logging
import os

logger = logging.getLogger("lpu5-api")


def generate_self_signed_cert(
    cert_path: str,
    key_path: str,
    hostname: str = "localhost",
) -> bool:
    """Generate a self-signed X.509 certificate and RSA private key.

    Parameters
    ----------
    cert_path : str
        Destination file path for the PEM-encoded certificate.
    key_path : str
        Destination file path for the PEM-encoded private key.
    hostname : str
        Common-Name / SAN for the certificate (usually the server IP).

    Returns
    -------
    bool
        ``True`` if the files were written successfully, ``False`` otherwise.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import ipaddress
    except ImportError:
        logger.warning(
            "cryptography package not available – cannot generate certificates"
        )
        return False

    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        subject = issuer = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, hostname)]
        )

        san_entries = [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        ]
        # Add the provided hostname as a SAN entry
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(hostname)))
        except ValueError:
            san_entries.append(x509.DNSName(hostname))

        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName(san_entries),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        with open(key_path, "wb") as f:
            f.write(
                key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )

        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        logger.info("Generated self-signed certificate: %s", cert_path)
        return True

    except Exception as exc:
        logger.error("Certificate generation failed: %s", exc)
        # Clean up partial files
        for path in (cert_path, key_path):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        return False


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    cert = "cert.pem"
    key = "key.pem"
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    if generate_self_signed_cert(cert, key, host):
        print(f"Certificate written to {cert} and {key}")
    else:
        print("Failed to generate certificate", file=sys.stderr)
        sys.exit(1)
