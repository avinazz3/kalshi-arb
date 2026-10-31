"""Local signing for authenticated, read-only Kalshi REST data requests."""
import base64
import os
from pathlib import Path
import time


def load_kalshi_auth():
    """Return a GET-path signer without recording credentials or key paths."""
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        raise ValueError("Kalshi data access requires local KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError as exc:
        raise RuntimeError("Install the [data] optional dependency") from exc
    try:
        private_key = load_pem_private_key(Path(key_path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        raise ValueError("Could not load the local Kalshi key; check its path and PEM format") from None
    signer = lambda path: kalshi_headers(key_id, private_key, path=path)
    signer("/trade-api/v2/cfbenchmarks/latest_values")  # Validate key type locally.
    return signer


def kalshi_headers(key_id, private_key, timestamp_ms=None, *, path):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa, ed25519
    if (not path.startswith("/trade-api/v2/") and path != "/trade-api/ws/v2") or "?" in path or "#" in path:
        raise ValueError("Sign a Kalshi REST API path without query parameters")
    timestamp = str(int(time.time() * 1000) if timestamp_ms is None else timestamp_ms)
    message = (timestamp + "GET" + path).encode()
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        signature = private_key.sign(message)
    elif isinstance(private_key, rsa.RSAPrivateKey):
        signature = private_key.sign(message, padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        raise ValueError("Kalshi key must be RSA or Ed25519")
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode()}
