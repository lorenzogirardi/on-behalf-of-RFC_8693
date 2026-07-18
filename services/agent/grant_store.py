"""Grant store — Redis-backed, AES-256-GCM encrypted. Identico alla produzione."""
from __future__ import annotations

import base64
import json
import os
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from obo import Grant, OBOClient

try:
    import redis as _redis_lib
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False


class GrantStore:
    def __init__(self, *, obo: OBOClient, aead_key: bytes,
                 redis_url: str = "redis://redis:6379") -> None:
        if len(aead_key) != 32:
            raise ValueError("aead_key must be 32 bytes")
        self._obo = obo
        self._aead = AESGCM(aead_key)
        self._prefix = "obo-grant:"
        self._trace_prefix = "obo-trace:"
        self._mem: dict = {}  # fallback in-memory if Redis unavailable
        self._redis = None
        if _REDIS_AVAILABLE:
            try:
                self._redis = _redis_lib.from_url(redis_url, decode_responses=True)
                self._redis.ping()
            except Exception as e:
                print(f"[STORE] Redis unavailable ({e}), using in-memory fallback", flush=True)
                self._redis = None

    def _seal(self, grant: Grant) -> str:
        nonce = os.urandom(12)
        pt = json.dumps(grant.to_dict()).encode()
        ct = self._aead.encrypt(nonce, pt, None)
        return base64.b64encode(nonce + ct).decode()

    def _open(self, blob: str) -> Grant:
        raw = base64.b64decode(blob)
        pt = self._aead.decrypt(raw[:12], raw[12:], None)
        return Grant.from_dict(json.loads(pt))

    def save(self, instance_id: str, grant: Grant) -> None:
        blob = self._seal(grant)
        key = self._prefix + instance_id
        if self._redis:
            self._redis.set(key, blob, ex=86400)
        else:
            self._mem[key] = blob

    def load(self, instance_id: str) -> Grant | None:
        key = self._prefix + instance_id
        blob = self._redis.get(key) if self._redis else self._mem.get(key)
        if not blob:
            return None
        return self._open(blob)

    def token_for_instance(self, instance_id: str) -> str:
        grant = self.load(instance_id)
        if grant is None:
            raise RuntimeError(f"no OBO grant for instance {instance_id}")
        if not grant.near_expiry():
            return grant.access_token
        refreshed = self._obo.refresh(grant)
        self.save(instance_id, refreshed)
        return refreshed.access_token

    def ping(self) -> bool:
        """True if the backing Redis answers (False on in-memory fallback)."""
        if not self._redis:
            return False
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def append_trace(self, instance_id: str, entry: dict) -> None:
        # Redis list: RPUSH is atomic, so concurrent tool calls (or multiple
        # agent replicas) never lose entries the way GET+SET would.
        try:
            key = self._trace_prefix + instance_id
            if self._redis:
                self._redis.rpush(key, json.dumps(entry))
                self._redis.expire(key, 86400)
            else:
                items = self._mem.get(key, [])
                items = list(items) + [entry]
                self._mem[key] = items
        except Exception:
            pass

    def get_trace(self, instance_id: str) -> list:
        try:
            key = self._trace_prefix + instance_id
            if self._redis:
                return [json.loads(x) for x in self._redis.lrange(key, 0, -1)]
            return list(self._mem.get(key, []))
        except Exception:
            return []

    @classmethod
    def from_env(cls, obo: OBOClient) -> "GrantStore":
        key_b64 = os.getenv("AGENT_STATE_AEAD_KEY",
                            "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=")
        key = base64.b64decode(key_b64)
        return cls(obo=obo, aead_key=key,
                   redis_url=os.getenv("REDIS_URL", "redis://redis:6379"))
