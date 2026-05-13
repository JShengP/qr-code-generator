import hashlib
import secrets
import string

from sqlalchemy.orm import Session

from .models import UrlMapping

BASE62_CHARS = string.ascii_letters + string.digits  # a-zA-Z0-9
TOKEN_LENGTH = 7
MAX_RETRIES = 10


def base62_encode(data: bytes) -> str:
    """Convert bytes to Base62 string."""
    num = int.from_bytes(data, "big")
    if num == 0:
        return BASE62_CHARS[0]
    result = []
    while num > 0:
        num, remainder = divmod(num, 62)
        result.append(BASE62_CHARS[remainder])
    return "".join(reversed(result))


def token_exists_in_db(db: Session, token: str) -> bool:
    return db.query(UrlMapping).filter(UrlMapping.token == token).first() is not None


def generate_token(url: str, db: Session) -> str:
    """SHA-256(url + CSPRNG nonce) → Base62 → first 7 chars, retried on collision.

    Why hash the URL alongside a random nonce: keeping the URL inside the
    digest input lets SHA-256's avalanche amplify the 8-byte nonce, so each
    retry samples a fresh point in the 7-char Base62 space (62^7 ≈ 3.5T)
    rather than a thin slice keyed off the URL alone.

    Why `secrets.token_bytes` over `time.time()`: a timestamp nonce makes
    two concurrent requests for the same URL within one second produce
    identical digests and burn every retry. CSPRNG nonces are independent
    across attempts and processes — collisions only come from the Base62
    truncation, which is exactly what `token_exists_in_db` catches.
    """
    for _ in range(MAX_RETRIES):
        nonce = secrets.token_bytes(8).hex()
        digest = hashlib.sha256(f"{url}|{nonce}".encode()).digest()
        token = base62_encode(digest)[:TOKEN_LENGTH]

        if not token_exists_in_db(db, token):
            return token

    raise RuntimeError(f"Failed to generate unique token after {MAX_RETRIES} retries")


def generate_edit_token() -> tuple[str, str]:
    """Return `(plaintext, sha256_hex_hash)` for a new edit token.

    The plaintext is 32 bytes from the OS CSPRNG, URL-safe Base64
    encoded — ~256 bits of entropy, well past brute-force range even
    without rate limiting on the bearer-check path. Only the hash is
    persisted; the plaintext goes back to the caller in the create
    response and must be held by them to perform PATCH or DELETE.
    """
    plaintext = secrets.token_urlsafe(32)
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, digest
