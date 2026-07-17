import hashlib
import hmac
import secrets

HASH_ALGORITHM = "pbkdf2_sha256"
HASH_ITERATIONS = 600_000
SALT_BYTES = 16


def _derive_password_hash(password: str, salt: bytes, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return digest.hex()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    password_hash = _derive_password_hash(password, salt, HASH_ITERATIONS)
    return f"{HASH_ALGORITHM}${HASH_ITERATIONS}${salt.hex()}${password_hash}"


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        algorithm, iterations_value, salt_value, expected_hash = hashed_password.split("$")
        iterations = int(iterations_value)
        salt = bytes.fromhex(salt_value)
    except ValueError:
        return False

    if algorithm != HASH_ALGORITHM:
        return False

    actual_hash = _derive_password_hash(password, salt, iterations)
    return hmac.compare_digest(actual_hash, expected_hash)
