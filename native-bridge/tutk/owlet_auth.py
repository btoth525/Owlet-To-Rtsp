"""
owlet_auth.py — turn your Owlet login into the camera's Kalay UID + AuthKey.

This is the "enter your credentials, it just works" layer (the wyze-bridge UX).
It logs into the Owlet cloud and reads back the camera's TUTK UID + AuthKey,
so the container is configured with nothing but OWLET_EMAIL / OWLET_PASSWORD.

==================== FILL FROM THE CAPTURE ====================
The exact endpoints/fields below are placeholders. Get the real ones from the
mitmproxy capture (native-bridge/mitm) — look in captures/ for the login call,
the token, the device-list call, and the response field(s) holding the UID and
AuthKey. Wire them into the three functions below.

Owlet's stack historically uses Firebase/AWS auth + a region API; the device
list returns per-camera P2P info. The capture tells you precisely.
==============================================================
"""

from __future__ import annotations

import os

import requests

OWLET_REGION = os.environ.get("OWLET_REGION", "world")  # "world" or "europe"

# TODO(capture): real values from captures/*.json
AUTH_URL = os.environ.get("OWLET_AUTH_URL", "https://REPLACE/login")
DEVICES_URL = os.environ.get("OWLET_DEVICES_URL", "https://REPLACE/devices")
API_KEY = os.environ.get("OWLET_API_KEY", "")           # if the app sends one


def login(email: str, password: str) -> str:
    """Return a bearer/id token. Shape per the captured login response."""
    resp = requests.post(
        AUTH_URL,
        json={"email": email, "password": password, "returnSecureToken": True},
        headers={"X-API-Key": API_KEY} if API_KEY else {},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    # TODO(capture): correct field, e.g. data["idToken"] / data["access_token"]
    return data.get("idToken") or data.get("access_token") or data["token"]


def list_devices(token: str) -> list[dict]:
    resp = requests.get(
        DEVICES_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    resp.raise_for_status()
    # TODO(capture): correct path to the device array
    data = resp.json()
    return data.get("devices") or data.get("response", {}).get("devices") or []


def get_camera_credentials(name: str | None = None) -> dict:
    """High-level: login + pick the camera + return {uid, authkey}."""
    email = os.environ["OWLET_EMAIL"]
    password = os.environ["OWLET_PASSWORD"]
    want = name or os.environ.get("OWLET_CAMERA_NAME")

    token = login(email, password)
    devices = list_devices(token)
    if not devices:
        raise RuntimeError("no Owlet devices returned for this account")

    cam = None
    for d in devices:
        if want and want.lower() not in str(d).lower():
            continue
        # TODO(capture): the fields that hold the Kalay UID + AuthKey
        uid = d.get("uid") or d.get("p2p_id") or d.get("kalay_uid")
        if uid:
            cam = {"uid": uid, "authkey": d.get("authKey") or d.get("auth_key")}
            break

    if not cam:
        raise RuntimeError(
            "could not find a Kalay UID in the device list — re-check the "
            "capture and update the field names in owlet_auth.py")
    return cam


if __name__ == "__main__":
    import json
    print(json.dumps(get_camera_credentials(), indent=2))
