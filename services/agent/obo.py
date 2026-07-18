"""OBO client — talks to obo-exchange service, which calls Keycloak."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import httpx

EXPIRY_SKEW = 60.0


@dataclass
class Grant:
    access_token: str
    refresh_token: str
    expires_at: float

    def near_expiry(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at - EXPIRY_SKEW

    def to_dict(self) -> dict:
        return {"access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at}

    @classmethod
    def from_dict(cls, d: dict) -> "Grant":
        return cls(d["access_token"], d.get("refresh_token", ""), float(d["expires_at"]))


def _grant_from_response(body: dict, now: float | None = None) -> Grant:
    now = now or time.time()
    return Grant(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", ""),
        expires_at=now + float(body.get("expires_in", 3600)),
    )


class OBOClient:
    """
    Mints and renews OBO grants via obo-exchange.
    The agent's own actor_token (Keycloak client_credentials) is cached here
    and used as the Authorization bearer on /exchange calls.
    """

    def __init__(self, *, kc_token_url: str, obo_exchange_base: str,
                 agent_client_id: str, agent_client_secret: str,
                 scope: str = "openid profile email offline_access",
                 timeout: float = 15.0) -> None:
        self._kc_token_url = kc_token_url
        base = obo_exchange_base.rstrip("/")
        self._exchange_url = base + "/exchange"
        self._refresh_url  = base + "/refresh"
        self._agent_client_id     = agent_client_id
        self._agent_client_secret = agent_client_secret
        self._scope = scope
        self._http = httpx.Client(timeout=timeout)

        self._lock = threading.Lock()
        self._actor_token: str | None = None
        self._actor_exp: float = 0.0

    def actor_token(self) -> str:
        """Get a valid agent client_credentials token from Keycloak (cached, auto-refreshed)."""
        with self._lock:
            if self._actor_token and time.time() < self._actor_exp - EXPIRY_SKEW:
                return self._actor_token
            resp = self._http.post(
                self._kc_token_url,
                data={"grant_type": "client_credentials",
                      "client_id": self._agent_client_id,
                      "client_secret": self._agent_client_secret,
                      "scope": "openid"},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            body = resp.json()
            self._actor_token = body["access_token"]
            self._actor_exp = time.time() + float(body.get("expires_in", 3600))
            return self._actor_token

    def exchange(self, subject_token: str) -> Grant:
        """Mint a renewable OBO grant for a user access token."""
        resp = self._http.post(
            self._exchange_url,
            data={"subject_token": subject_token, "scope": self._scope},
            headers={"Authorization": f"Bearer {self.actor_token()}",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
        )
        resp.raise_for_status()
        return _grant_from_response(resp.json())

    def refresh(self, grant: Grant) -> Grant:
        """Renew via obo-exchange /refresh. Refresh token rotates."""
        resp = self._http.post(
            self._refresh_url,
            data={"refresh_token": grant.refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
        )
        resp.raise_for_status()
        return _grant_from_response(resp.json())

    @classmethod
    def from_env(cls) -> "OBOClient":
        # Canonical names first; legacy ZITADEL_* names kept as fallbacks.
        kc_issuer = (os.getenv("KC_ISSUER") or os.getenv("ZITADEL_ISSUER")
                     or "http://keycloak:8080/realms/poc")
        kc_token_url = os.getenv("KC_TOKEN_URL",
                                 kc_issuer.rstrip("/") + "/protocol/openid-connect/token")
        return cls(
            kc_token_url=kc_token_url,
            obo_exchange_base=os.getenv("OBO_EXCHANGE_BASE", "http://obo-exchange:8081"),
            agent_client_id=(os.getenv("AGENT_CLIENT_ID")
                             or os.getenv("ZITADEL_AGENT_CLIENT_ID") or "agent-service"),
            agent_client_secret=(os.getenv("AGENT_CLIENT_SECRET")
                                 or os.getenv("ZITADEL_AGENT_CLIENT_SECRET") or "agent-service-secret"),
            scope=os.getenv("OBO_SCOPE", "openid profile email offline_access"),
        )
