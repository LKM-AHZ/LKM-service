import time
import uuid

import jwt
import pytest

from app.core.config import settings
from app.core.secrets import reveal
from app.modules.auth.security import (
    create_access_token,
    create_temp_token,
    decode_access_token,
    decode_temp_token,
    decrypt_secret,
    dummy_verify,
    encrypt_secret,
    generate_recovery_codes,
    generate_totp_secret,
    get_totp_uri,
    hashpwd,
    verify_totp,
    verifypwd,
)

# ---------------------------------------------------------------------------
# JWT – access token
# ---------------------------------------------------------------------------


class TestAccessToken:
    def should_create_and_decode(self):
        uid = uuid.uuid4()
        token = create_access_token(user_id=uid, account_level="normal", role="member")
        payload = decode_access_token(token)
        assert payload["user_id"] == str(uid)
        assert payload["account_level"] == "normal"
        assert payload["role"] == "member"
        assert payload["type"] == "access"

    def should_reject_wrong_secret(self):
        token = create_access_token(
            user_id=uuid.uuid4(), account_level="normal", role="member"
        )
        wrong_key = "wrong-secret-key-hopefully-not-used"
        with pytest.raises(jwt.exceptions.InvalidSignatureError):
            jwt.decode(token, wrong_key, algorithms=[settings.jwt_algorithm])

    def should_reject_expired_token(self):
        # Build an already-expired JWT manually
        now = int(time.time())
        payload = {
            "user_id": str(uuid.uuid4()),
            "account_level": "normal",
            "role": "member",
            "type": "access",
            "iat": now - 9999,
            "exp": now - 3600,  # expired 1 hour ago
        }
        token = jwt.encode(
            payload, reveal(settings.jwt_secret), algorithm=settings.jwt_algorithm
        )
        with pytest.raises(jwt.exceptions.ExpiredSignatureError):
            decode_access_token(token)

    def should_reject_non_access_type(self):
        # 传入 temp token：其 audience 与 access 不同，单次验 aud 即被拒（不再是"先看 type"）
        token = create_temp_token(user_id=uuid.uuid4())
        with pytest.raises((jwt.exceptions.InvalidAudienceError, ValueError)):
            decode_access_token(token)


# ---------------------------------------------------------------------------
# JWT – temp token
# ---------------------------------------------------------------------------


class TestTempToken:
    def should_create_and_decode(self):
        uid = uuid.uuid4()
        token = create_temp_token(user_id=uid)
        payload = decode_temp_token(token)
        assert payload["user_id"] == str(uid)
        assert payload["type"] == "temp"

    def should_reject_non_temp_type(self):
        # 传入 access token：audience 与 temp 不同，单次验 aud 即被拒
        token = create_access_token(
            user_id=uuid.uuid4(), account_level="normal", role="member"
        )
        with pytest.raises((jwt.exceptions.InvalidAudienceError, ValueError)):
            decode_temp_token(token)


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


class TestTOTP:
    def should_generate_valid_secret(self):
        secret = generate_totp_secret()
        assert len(secret) >= 16  # base32 encoding of 20 bytes
        # should be base32 decodable
        import base64

        base64.b32decode(secret, casefold=True)

    def should_generate_uri(self):
        secret = generate_totp_secret()
        uri = get_totp_uri(secret, "alice", "TestIssuer")
        assert uri.startswith("otpauth://totp/")
        assert "alice" in uri
        assert "TestIssuer" in uri

    def should_verify_valid_code(self):
        secret = generate_totp_secret()
        # Generate a valid TOTP code from secret for time step now
        import base64
        import hashlib
        import hmac
        import struct

        now = int(time.time()) // 30
        key = base64.b32decode(secret, casefold=True)
        msg = struct.pack(">Q", now)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        code = (struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
        code_str = f"{code:06d}"

        assert verify_totp(secret, code_str, window=0) is not None

    def should_reject_wrong_code(self):
        secret = generate_totp_secret()
        assert verify_totp(secret, "000000", window=0) is None

    def should_accept_code_within_window(self):
        secret = generate_totp_secret()
        import base64
        import hashlib
        import hmac
        import struct

        # Generate code for previous time step (now - 30)
        prev_time = int(time.time()) // 30 - 1
        key = base64.b32decode(secret, casefold=True)
        msg = struct.pack(">Q", prev_time)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        code = (struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
        code_str = f"{code:06d}"

        assert verify_totp(secret, code_str, window=1) is not None


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------


class TestRecoveryCodes:
    def should_generate_n_codes(self):
        codes = generate_recovery_codes(10)
        assert len(codes) == 10

    def should_be_unique(self):
        codes = generate_recovery_codes(100)
        plains = [c[0] for c in codes]
        hashes = [c[1] for c in codes]
        assert len(set(plains)) == 100
        assert len(set(hashes)) == 100

    def should_have_correct_tuple_structure(self):
        codes = generate_recovery_codes(5)
        for plain, hashed in codes:
            assert isinstance(plain, str)
            assert isinstance(hashed, str)
            assert len(plain) > 0
            assert len(hashed) > 0
            assert plain != hashed


# ---------------------------------------------------------------------------
# Encrypt / Decrypt
# ---------------------------------------------------------------------------


class TestEncryptDecrypt:
    def should_roundtrip_secret(self):
        plain = "JBSWY3DPEHPK3PXP"
        cipher = encrypt_secret(plain)
        assert cipher != plain
        assert decrypt_secret(cipher) == plain

    def should_produce_different_ciphertexts(self):
        plain = "JBSWY3DPEHPK3PXP"
        c1 = encrypt_secret(plain)
        c2 = encrypt_secret(plain)
        # AES-GCM uses random nonce, so ciphertexts should differ
        assert c1 != c2
        assert decrypt_secret(c1) == plain
        assert decrypt_secret(c2) == plain


# ---------------------------------------------------------------------------
# Password hash (argon2) —— 必须异步化，offload 到线程池避免阻塞事件循环
# ---------------------------------------------------------------------------


class TestPasswordHash:
    async def should_hash_and_verify_async(self):
        import asyncio

        assert asyncio.iscoroutinefunction(hashpwd)
        hashed = await hashpwd("secret123456")
        assert hashed != "secret123456"
        assert await verifypwd("secret123456", hashed)
        assert not await verifypwd("wrong-password", hashed)

    async def should_dummy_verify_async(self):
        import asyncio

        assert asyncio.iscoroutinefunction(dummy_verify)
        await dummy_verify()
