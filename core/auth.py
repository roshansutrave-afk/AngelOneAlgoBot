"""
core/auth.py
Session lifecycle manager for AngelOne SmartAPI.

SmartAPI JWT tokens expire after a few hours; refreshToken is meant to
mint new ones without a full re-login. The refresh response shape is
thinly documented across SDK versions, so this wraps it defensively —
any failure falls back to a full re-login rather than crashing the
main loop.
"""
from __future__ import annotations
import time
from dataclasses import dataclass
import pyotp
from SmartApi import SmartConnect

from config.settings import AngelOneCredentials


@dataclass
class SessionTokens:
    jwt_token: str
    refresh_token: str
    feed_token: str
    issued_at: float


class AngelOneSession:
    """
    Owns one authenticated SmartConnect client + its token lifecycle.
    Not thread-safe by design — run one instance per process and pass
    it around; don't share across threads without a lock.
    """

    def __init__(self, creds: AngelOneCredentials, logger):
        self._creds = creds
        self._log = logger
        self.client = SmartConnect(api_key=creds.api_key)
        self.tokens: SessionTokens | None = None

    def login(self) -> SessionTokens:
        totp = pyotp.TOTP(self._creds.totp_secret).now()
        resp = self.client.generateSession(self._creds.client_code, self._creds.pin, totp)

        if not resp.get("status"):
            raise RuntimeError(f"AngelOne login failed: {resp}")

        data = resp["data"]
        feed_token = self.client.getfeedToken()
        self.tokens = SessionTokens(
            jwt_token=data["jwtToken"],
            refresh_token=data["refreshToken"],
            feed_token=feed_token,
            issued_at=time.time(),
        )
        self._log.info("AngelOne session established for client %s", self._creds.client_code)
        return self.tokens

    def refresh(self) -> SessionTokens:
        if self.tokens is None:
            return self.login()
        try:
            resp = self.client.generateToken(self.tokens.refresh_token)
            if not resp.get("status"):
                raise RuntimeError(str(resp))
            data = resp["data"]
            self.tokens = SessionTokens(
                jwt_token=data["jwtToken"],
                refresh_token=data.get("refreshToken", self.tokens.refresh_token),
                feed_token=self.tokens.feed_token,
                issued_at=time.time(),
            )
            self._log.info("AngelOne session token refreshed")
        except Exception as exc:
            self._log.warning("Token refresh failed (%s) — falling back to full re-login", exc)
            return self.login()
        return self.tokens

    def ensure_fresh(self, max_age_seconds: int = 5 * 60 * 60) -> SessionTokens:
        if self.tokens is None or (time.time() - self.tokens.issued_at) > max_age_seconds:
            return self.refresh() if self.tokens else self.login()
        return self.tokens

    def logout(self) -> None:
        if self.tokens:
            self.client.terminateSession(self._creds.client_code)
            self._log.info("AngelOne session terminated")
