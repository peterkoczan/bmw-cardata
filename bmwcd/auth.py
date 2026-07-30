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


class TokenStore:
    def __init__(self, path: Path, client_id: str):
        self.path = path
        self.client_id = client_id

    def load(self) -> Tokens | None:
        if not self.path.exists():
            return None
        return Tokens(json.loads(self.path.read_text()))

    def save(self, data: dict) -> Tokens:
        # 0600 -- refresh token is a two-week credential.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        return Tokens(data)

    def refresh(self, tokens: Tokens) -> Tokens:
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
        if resp.status_code != 200:
            raise RuntimeError(
                f"Refresh failed ({resp.status_code}): {resp.text}\n"
                "If the refresh token has expired (~2 weeks), re-run: bmwcd auth"
            )
        body = resp.json()
        # BMW does not always echo gcid on refresh; keep the one we have.
        body.setdefault("gcid", tokens.gcid)
        return self.save(body)

    def fresh(self) -> Tokens:
        """Return tokens with an id_token good for at least REFRESH_MARGIN."""
        tokens = self.load()
        if tokens is None:
            raise SystemExit("No tokens. Run: bmwcd auth")
        if tokens.seconds_left() < REFRESH_MARGIN:
            tokens = self.refresh(tokens)
        return tokens


def device_flow(store: TokenStore) -> Tokens:
    """Interactive device-code authorisation. Blocks until the user approves."""
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
    auth = resp.json()

    # BMW returns verification_uri + a separate user_code; the RFC-8628
    # convenience form (verification_uri_complete) is not offered, so the code
    # has to be typed in by hand.
    uri = auth.get("verification_uri_complete") or auth["verification_uri"]
    print()
    print("  Open this URL and log in with your BMW ID:")
    print(f"    {uri}")
    print(f"  Enter user code: {auth['user_code']}")
    print()
    print("  Finish the browser login completely before doing anything here.")
    print("  Waiting for approval...")

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
