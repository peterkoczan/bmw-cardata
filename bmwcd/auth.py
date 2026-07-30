"""OAuth2 device-code flow against BMW GCDM, with PKCE.

Endpoints and payload shapes confirmed against
whi-tw/bmw-cardata-streaming-poc AUTHENTICATION.md. Do not guess these.
"""

import base64
import hashlib
import json
import os
import time
from pathlib import Path

import requests

DEVICE_CODE_URL = "https://customer.bmwgroup.com/gcdm/oauth/device/code"
TOKEN_URL = "https://customer.bmwgroup.com/gcdm/oauth/token"
SCOPE = "authenticate_user openid cardata:streaming:read cardata:api:read"

# Reconnect this long before the id_token actually expires.
REFRESH_MARGIN = 300


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _jwt_exp(token: str) -> int:
    """Read `exp` out of a JWT without verifying it.

    The id_token is the MQTT password, so its own exp is what governs when we
    have to reconnect -- not the access_token's expires_in.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])


class Tokens:
    def __init__(self, data: dict):
        self.data = data

    @property
    def id_token(self) -> str:
        return self.data["id_token"]

    @property
    def access_token(self) -> str:
        return self.data["access_token"]

    @property
    def refresh_token(self) -> str:
        return self.data["refresh_token"]

    @property
    def gcid(self) -> str:
        return self.data["gcid"]

    @property
    def expires_at(self) -> int:
        return _jwt_exp(self.id_token)

    def seconds_left(self) -> float:
        return self.expires_at - time.time()


class AuthRetryable(Exception):
    """Transient refresh failure -- network or BMW-side. Back off, do not exit."""


class TokenStore:
    def __init__(self, path: Path, client_id: str):
        self.path = path
        self.client_id = client_id

    def load(self) -> Tokens | None:
        if not self.path.exists():
            return None
        return Tokens(json.loads(self.path.read_text()))

    def save(self, data: dict) -> Tokens:
        # Write-then-rename. BMW rotates the refresh token on every refresh, so
        # truncating the real file first means a crash mid-write destroys the
        # only copy of a two-week credential and forces an interactive re-auth
        # -- painful on a headless host. 0600: it is a credential.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        return Tokens(data)

    def refresh(self, tokens: Tokens) -> Tokens:
        try:
            resp = requests.post(
                TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh_token,
                    "client_id": self.client_id,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            # DNS blip, TLS reset, timeout. Nothing is wrong with the credential.
            raise AuthRetryable(f"refresh request failed: {exc}") from exc

        if resp.status_code != 200:
            body = resp.text
            fatal = "invalid_grant" in body or "invalid_token" in body
            # 5xx and 429 are BMW's problem and will pass; anything else 4xx
            # means the credential or the client is wrong and a human is needed.
            if not fatal and (resp.status_code >= 500 or resp.status_code == 429):
                raise AuthRetryable(f"refresh failed ({resp.status_code}), retrying")
            raise SystemExit(
                f"Refresh rejected ({resp.status_code}): {body}\n"
                "If the refresh token has expired (~2 weeks), re-run: bmwcd auth"
            )
        body = resp.json()
        # Carry forward anything BMW omits. gcid is routinely absent on refresh;
        # refresh_token is normally rotated but must never be *lost* -- saving a
        # body without it writes a token file we cannot refresh from, which
        # costs an interactive re-auth even though the old credential was still
        # perfectly good.
        for field in ("gcid", "refresh_token"):
            body.setdefault(field, tokens.data.get(field))

        missing = [f for f in ("id_token", "refresh_token", "gcid") if not body.get(f)]
        if missing:
            # Do not overwrite a working tokens.json with an unusable one.
            raise AuthRetryable(f"refresh response missing {missing}; keeping old tokens")
        return self.save(body)

    def fresh(self) -> Tokens:
        """Return tokens with an id_token good for at least REFRESH_MARGIN."""
        tokens = self.load()
        if tokens is None:
            raise SystemExit("No tokens. Run: bmwcd auth")
        if tokens.seconds_left() < REFRESH_MARGIN:
            tokens = self.refresh(tokens)
        return tokens


def request_device_code(store: TokenStore) -> tuple[dict, str]:
    """Ask BMW for a device code. Fast; safe to call on a UI thread.

    Split out from the polling half so a GUI can show the code immediately and
    poll in the background, rather than blocking for up to five minutes.
    """
    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())

    resp = requests.post(
        DEVICE_CODE_URL,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "client_id": store.client_id,
            "response_type": "device_code",
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json(), verifier


def verification_uri(auth: dict) -> str:
    # BMW returns verification_uri + a separate user_code; the RFC-8628
    # convenience form (verification_uri_complete) is not offered, so the code
    # has to be typed in by hand.
    return auth.get("verification_uri_complete") or auth["verification_uri"]


def poll_for_tokens(store: TokenStore, auth: dict, verifier: str) -> Tokens:
    """Poll until the user approves in the browser, or the code expires."""
    interval = int(auth.get("interval", 5))
    deadline = time.time() + int(auth.get("expires_in", 300))

    while time.time() < deadline:
        time.sleep(interval)
        poll = requests.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id": store.client_id,
                "device_code": auth["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "code_verifier": verifier,
            },
            timeout=30,
        )
        if poll.status_code == 200:
            body = poll.json()
            _check_scopes(body)
            return store.save(body)

        try:
            err = poll.json().get("error", "")
        except ValueError:
            err = poll.text
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise SystemExit(f"Authorisation failed ({poll.status_code}): {poll.text}")

    raise SystemExit("Device code expired before approval. Re-run: bmwcd auth")


def device_flow(store: TokenStore) -> Tokens:
    """Terminal device-code authorisation. Blocks until the user approves."""
    auth, verifier = request_device_code(store)
    print()
    print("  Open this URL and log in with your BMW ID:")
    print(f"    {verification_uri(auth)}")
    print(f"  Enter user code: {auth['user_code']}")
    print()
    print("  Finish the browser login completely before doing anything here.")
    print("  Waiting for approval...")
    return poll_for_tokens(store, auth, verifier)


def _check_scopes(body: dict) -> None:
    """Catch the silent-failure case the portal sets up.

    If the client ID was never subscribed to CarData Streaming, auth still
    succeeds but comes back without the streaming scope -- and MQTT then
    rejects the credentials with no useful error.
    """
    granted = set(body.get("scope", "").split())
    if "cardata:streaming:read" not in granted:
        raise SystemExit(
            "Auth succeeded but 'cardata:streaming:read' was not granted.\n"
            f"Granted: {sorted(granted) or '(none)'}\n"
            "Subscribe the client ID to CarData Streaming in the My BMW portal, "
            "then delete tokens.json and re-run: bmwcd auth"
        )
