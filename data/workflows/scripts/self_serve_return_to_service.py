#!/usr/bin/env python3
"""Return to Service, started from the device itself.

Trigger: JAWA self-serve (a web clip on the device). The payload
names ONE device by Jamf Pro id and UDID. This script:
1. Fetches the device record by id and refuses unless the UDID
   matches (the caller must be holding that device -- ADR-0012).
2. Sends ERASE_DEVICE with returnToService so the device wipes and
   re-enrolls onto the configured Wi-Fi with no IT touch.

Exit codes: 0 sent; 12 bad payload; 20 UDID mismatch; 21 device not
found; 22 device has no managementId; 23 erase command refused.
"""

import base64
import json
import sys
import time

import requests

# --- JAWA canonical Jamf API block (keep identical across templates) ---

token_cache = {"access_token": None, "expires_in": 0, "timestamp": 0}


class Config:
    def __init__(self):
        self.server_url = "__JAWA_SERVER_URL__"
        self.token_url = f"{self.server_url}/api/oauth/token"
        self.client_id = "__JAWA_CLIENT_ID__"
        self.client_secret = "__JAWA_CLIENT_SECRET__"
        self.scope = ""


def get_oauth_token():
    """Fetch (and cache) an OAuth access token from Jamf Pro."""
    global token_cache
    config = Config()
    current_time = time.time()
    if (
        token_cache["access_token"]
        and (current_time - token_cache["timestamp"])
        < token_cache["expires_in"]
    ):
        return token_cache["access_token"]
    data = {
        "client_id": config.client_id,
        "grant_type": "client_credentials",
        "client_secret": config.client_secret,
    }
    if config.scope:
        data["scope"] = config.scope
    response = requests.post(
        config.token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=data,
        timeout=30,
    )
    response.raise_for_status()
    response_data = response.json()
    token_cache = {
        "access_token": response_data["access_token"],
        "expires_in": response_data["expires_in"],
        "timestamp": current_time,
    }
    return token_cache["access_token"]


def perform_api_call(endpoint, method="GET", data=None, api="classic"):
    """Call the Jamf Pro API and return the parsed response.

    endpoint: path with no leading slash, e.g. "mobiledevices/id/42".
    api: "classic" for /JSSResource, "pro" for /api.
    Returns parsed JSON, or the raw response when the body is not JSON.
    """
    config = Config()
    token = get_oauth_token()
    prefix = "JSSResource" if api == "classic" else "api"
    url = f"{config.server_url}/{prefix}/{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    response = requests.request(
        method, url, headers=headers, json=data, timeout=30
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        return response

# --- end canonical block ---


# WiFi profile XML (base64 will be encoded at runtime)
WIFI_PAYLOAD = b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1"><dict>
  <key>PayloadUUID</key><string>RTS-UNIQUE-UUID</string>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadIdentifier</key><string>RTS-UNIQUE-UUID</string>
  <key>PayloadContent</key><array><dict>
    <key>PayloadType</key><string>com.apple.wifi.managed</string>
    <key>SSID_STR</key><string>__JAWA_WIFI_SSID__</string>
    <key>AutoJoin</key><true/>
    <key>CaptiveBypass</key><true/>
  </dict></array>
</dict></plist>"""


def fetch_verified_device(jss_id, udid):
    """The device record, only if Jamf Pro's UDID matches the caller's.

    The web clip URL is caller-supplied, so the id alone must never be
    enough to erase a device. Exit 21 if the id is unknown, 20 if the
    UDID does not match.
    """
    try:
        device = perform_api_call(f"v2/mobile-devices/{jss_id}", api="pro")
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 404:
            print(f"Device {jss_id} not found in Jamf Pro.")
            sys.exit(21)
        raise
    actual = str(device.get("udid", "")).strip().lower()
    if not actual or actual != str(udid).strip().lower():
        print(f"UDID mismatch for device {jss_id}; refusing to erase.")
        sys.exit(20)
    return device


def main():
    event_data = json.loads(sys.argv[1])
    event = event_data.get("event") or {}
    jss_id = event.get("jssID")
    udid = event.get("udid")
    if not isinstance(jss_id, int) or not udid:
        print("Payload did not name a device id and UDID.")
        sys.exit(12)

    device = fetch_verified_device(jss_id, udid)
    mgmt_id = device.get("managementId")
    if not mgmt_id:
        print(f"Device {jss_id} has no managementId; cannot send MDM.")
        sys.exit(22)

    wifi_b64 = base64.b64encode(WIFI_PAYLOAD).decode("ascii")
    erase_json = {
        "clientData": [{"managementId": mgmt_id}],
        "commandData": {
            "commandType": "ERASE_DEVICE",
            "preserveDataPlan": True,
            "disallowProximitySetup": False,
            "returnToService": {
                "enabled": True,
                "wifiProfileData": wifi_b64,
            },
        },
    }
    try:
        perform_api_call("v2/mdm/commands", method="POST",
                         data=erase_json, api="pro")
    except requests.HTTPError as err:
        print(f"Erase command refused: {err}")
        sys.exit(23)
    print(f"Device {jss_id}: Return to Service erase sent.")


if __name__ == "__main__":
    main()
