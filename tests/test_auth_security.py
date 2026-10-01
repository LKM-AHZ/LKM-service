import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from auth import jwt_keys
from auth.security import (
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

    def should_reject_wrong_public_key(self):
        token = create_access_token(
            user_id=uuid.uuid4(), account_level="normal", role="member"
        )
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with pytest.raises(jwt.exceptions.InvalidSignatureError):
            jwt.decode(token, other.public_key(), algorithms=["RS256"])

    def should_reject_expired_token(self):
        # Build an already-expired JWT manually（用配置的私钥以 RS256 签）
        now = int(time.time())
        payload = {
            "user_id": str(uuid.uuid4()),
            "account_level": "normal",
            "role": "member",
            "type": "access",
            "aud": "lkm:web",
            "iat": now - 9999,
            "exp": now - 3600,  # expired 1 hour ago
        }
        token = jwt_keys.encode(payload)
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

    def should_use_versioned_format_with_per_record_salt(self):
        c1 = encrypt_secret("JBSWY3DPEHPK3PXP")
        c2 = encrypt_secret("JBSWY3DPEHPK3PXP")
        assert c1.startswith("v2:") and c2.startswith("v2:")
        # 每记录随机 16B 盐（base64 前 16B）→ 两次的盐段也不同，而非仅 nonce 不同
        assert c1[:24] != c2[:24]

    def should_reject_malformed_ciphertext(self):
        # 含无版本前缀的旧格式：不再兼容，一律按损坏拒
        for bad in ("not-valid-base64!!", "v2:AAAA", ""):
            with pytest.raises(ValueError, match="malformed ciphertext"):
                decrypt_secret(bad)


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
