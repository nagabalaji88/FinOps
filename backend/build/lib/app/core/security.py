"""Password hashing, JWT issuance/verification, API keys and TOTP MFA."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import pyotp
from jose import JWTError, jwt

from app.core.config import settings
from app.core.errors import AuthError

ACCESS = "access"
REFRESH = "refresh"
#: Carries a proposed TOTP secret between `/mfa/enrol` and `/mfa/verify`. Holding the
#: candidate in a signed, short-lived ticket instead of writing it straight to the user row
#: means an abandoned enrolment cannot strand an account whose existing factor was
#: overwritten -- the active secret is replaced only once the new one has been proven.
MFA_ENROL = "mfa_enrol"
MFA_ENROL_TTL_SECONDS = 600
BCRYPT_ROUNDS = 12


def _prehash(password: str) -> bytes:
    """SHA-256 + base64 so passwords of any length are handled by bcrypt's 72-byte limit."""
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), hashed.encode())
    except Exception:
        return False


def _encode(payload: dict[str, Any], ttl: int, token_type: str) -> str:
    now = datetime.now(UTC)
    body = {
        **payload,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        "jti": str(uuid.uuid4()),
        "typ": token_type,
        "iss": settings.app_name,
    }
    return jwt.encode(body, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(*, user_id: str, email: str, roles: list[str], scopes: list[str]) -> str:
    return _encode(
        {"sub": user_id, "email": email, "roles": roles, "scopes": scopes},
        settings.access_token_ttl_seconds,
        ACCESS,
    )


def create_refresh_token(*, user_id: str) -> str:
    return _encode({"sub": user_id}, settings.refresh_token_ttl_seconds, REFRESH)


def create_mfa_enrolment_token(*, user_id: str, secret: str) -> str:
    return _encode({"sub": user_id, "mfa_secret": secret}, MFA_ENROL_TTL_SECONDS, MFA_ENROL)


def decode_token(token: str, *, expected_type: str = ACCESS) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise AuthError("Invalid or expired token", details={"reason": str(exc)}) from exc
    if payload.get("typ") != expected_type:
        raise AuthError("Wrong token type")
    return payload


# --- API keys ---------------------------------------------------------------
API_KEY_PREFIX = "fops"


def generate_api_key() -> tuple[str, str, str]:
    """Returns (full_key, prefix, sha256_hash). The full key is shown once."""
    raw = secrets.token_urlsafe(32)
    prefix = secrets.token_hex(4)
    full = f"{API_KEY_PREFIX}_{prefix}_{raw}"
    return full, prefix, hash_api_key(full)


def hash_api_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --- MFA --------------------------------------------------------------------
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=settings.mfa_issuer)


def verify_totp(secret: str, code: str) -> bool:
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=1)
    except Exception:
        return False


# --- Field-level encryption for secrets at rest ------------------------------
#: Envelope format marker. Values written before this existed carry no marker and are
#: decrypted by :func:`_decrypt_legacy` so that an upgrade does not strand stored
#: credentials; nothing writes that format any more.
_ENVELOPE_V2 = "v2"
_AAD = b"finops.secret-at-rest"
_AES_GCM_NONCE_BYTES = 12


def _aead_key() -> bytes:
    """Stretch the configured key material into an AES-256 key.

    ``secret_encryption_key`` is deliberately separate from ``jwt_secret``: sharing them
    means one weak value compromises both the tokens and every provider credential at
    rest, and rotating the signing key silently makes the stored credentials undecryptable.
    The JWT fallback exists only so an existing deployment keeps working; production
    refuses to start on it.
    """
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    material, _ = settings.secret_encryption_material
    return HKDF(algorithm=SHA256(), length=32, salt=None, info=_AAD).derive(material.encode())


def encrypt_value(plaintext: str) -> str:
    """AES-256-GCM envelope; used for secrets at rest when Vault is not configured."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(_AES_GCM_NONCE_BYTES)
    sealed = AESGCM(_aead_key()).encrypt(nonce, plaintext.encode(), _AAD)
    return f"{_ENVELOPE_V2}.{base64.urlsafe_b64encode(nonce + sealed).decode()}"


def decrypt_value(token: str) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not token.startswith(f"{_ENVELOPE_V2}."):
        return _decrypt_legacy(token)
    blob = base64.urlsafe_b64decode(token.split(".", 1)[1].encode())
    nonce, sealed = blob[:_AES_GCM_NONCE_BYTES], blob[_AES_GCM_NONCE_BYTES:]
    try:
        return AESGCM(_aead_key()).decrypt(nonce, sealed, _AAD).decode()
    except InvalidTag as exc:
        raise AuthError("Secret integrity check failed") from exc


def _decrypt_legacy(token: str) -> str:
    """Read the pre-AEAD XOR/HMAC envelope, which was always keyed off ``jwt_secret``."""
    key = hashlib.sha256(settings.jwt_secret.encode()).digest()
    blob = base64.urlsafe_b64decode(token.encode())
    nonce, mac, ct = blob[:16], blob[16:32], blob[32:]
    expected = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(mac, expected):
        raise AuthError("Secret integrity check failed")
    stream = b""
    counter = 0
    while len(stream) < len(ct):
        stream += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(ct, stream, strict=False)).decode()


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * min(len(value) - keep, 24)}"
