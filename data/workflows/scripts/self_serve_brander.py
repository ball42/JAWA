#!/usr/bin/env python3
"""Brander, started from the device itself.

Trigger: JAWA self-serve (a web clip on the device). Generates a
wallpaper carrying the device's identifying details and a QR code of
its Jamf Pro id, then sets it on that device with the Wallpaper MDM
command. The payload names ONE device by id and UDID; the UDID must
match Jamf Pro's record before anything is sent (ADR-0012).

Originally written for webhook events by Chris Ball (2021), with
updates by David Raabe and Tim Knox.

Exit codes: 0 set; 12 bad payload; 20 UDID mismatch; 21 device not
found; 24 wallpaper command refused; 42 Brander EA is Off.
"""

import base64
import json
import os
import sys
import time

import qrcode
import requests
from PIL import Image, ImageDraw, ImageFont

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


# Template settings (filled in when the template is enabled).
EA_ID = __JAWA_EA_ID__  # noqa: F821 -- Jamf Setup role EA id; 0 = unused
WALLPAPER_SETTING = __JAWA_WALLPAPER_SETTING__  # noqa: F821 -- 1/2/3
ASSETS_DIR = "__JAWA_ASSETS_DIR__"
FONT_PATH = "__JAWA_FONT_PATH__"  # "none" or blank = Pillow's built-in font


def load_font(size):
    text = FONT_PATH.strip()
    if text and text.lower() != "none":
        return ImageFont.truetype(text, size)
    return ImageFont.load_default(size=size)


def fetch_verified_device(jss_id, udid):
    """Classic mobile device record, only if the UDID matches."""
    try:
        record = perform_api_call(f"mobiledevices/id/{jss_id}")
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 404:
            print(f"Device {jss_id} not found in Jamf Pro.")
            sys.exit(21)
        raise
    device = record.get("mobile_device", {})
    actual = str(device.get("general", {}).get("udid", "")).strip().lower()
    if not actual or actual != str(udid).strip().lower():
        print(f"UDID mismatch for device {jss_id}; refusing to brand.")
        sys.exit(20)
    return device


def find_role(device):
    """The wallpaper role: Jamf Setup EA value, or "Brander" EA switch.

    Returns (template_basename, role_label). A "Brander" EA set to Off
    exits 42, matching the original webhook Brander.
    """
    eas = device.get("extension_attributes", [])
    role = ""
    for ea in eas:
        if ea.get("name") == "Brander":
            value = (ea.get("value") or "").strip()
            if not value or value.lower() == "off":
                print("Brander EA is Off for this device; exiting.")
                sys.exit(42)
        if EA_ID and str(ea.get("id")) == str(EA_ID):
            role = (ea.get("value") or "").strip()
    if not role:
        return "null", ""
    basename = role.replace(" ", "").lower()
    if os.path.isfile(os.path.join(ASSETS_DIR, f"{basename}.png")):
        return basename, role
    return "lockscreen-template", role


def _centered(draw, font, text, img_w, y, color):
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((img_w - (right - left)) / 2, y), text, color, font=font)


def make_image(assets_dir, basename, role, device, ea_note, out_dir):
    """Compose the wallpaper and return the PNG path."""
    general = device.get("general", {})
    location = device.get("location", {})
    role_line = role.title() if role else "Please open the Setup app"
    if role.lower() in ("hold", "transfer"):
        role_line = "Awaiting location assignment"
    if role.lower() == "pending":
        role_line = "Please open the Reset app"

    lines = [
        str(general.get("device_name", "")),
        str(general.get("serial_number", "")),
        str(location.get("building", "")),
        str(location.get("department", "")),
        f"{general.get('model', '')} {general.get('os_type', '')} "
        f"{general.get('os_version', '')}".strip(),
        role_line,
    ]
    img = Image.open(os.path.join(assets_dir, f"{basename}.png")).convert(
        "RGB"
    )
    img_w, img_h = img.size
    draw = ImageDraw.Draw(img)
    font = load_font(max(12, img_h // 40))
    for index, line in enumerate(lines):
        _centered(draw, font, line, img_w, img_h * (0.60 + 0.05 * index),
                  (0, 0, 0))

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=4,
        border=1,
    )
    qr.add_data(str(general.get("id", "")))
    qr.make(fit=True)
    # qrcode's PilImage wrapper is not a PIL.Image.Image subclass, so
    # Image.paste cannot infer its size from a 2-tuple box; unwrap it.
    qr_img = qr.make_image(fill_color="black", back_color="white").get_image()
    img.paste(
        qr_img,
        ((img_w - qr_img.size[0]) // 2, (img_h - qr_img.size[1]) // 2),
    )
    out = os.path.join(out_dir, f"brander-{int(time.time() * 1000)}.png")
    img.save(out)
    return out


def set_wallpaper(image_path, jss_id):
    with open(image_path, "rb") as handle:
        b64 = base64.b64encode(handle.read()).decode("ascii")
    body = (
        "<mobile_device_command><command>Wallpaper</command>"
        f"<wallpaper_setting>{WALLPAPER_SETTING}</wallpaper_setting>"
        f"<wallpaper_content>{b64}</wallpaper_content>"
        f"<mobile_devices><mobile_device><id>{jss_id}</id></mobile_device>"
        "</mobile_devices></mobile_device_command>"
    )
    config = Config()
    resp = requests.post(
        f"{config.server_url}/JSSResource/mobiledevicecommands/command/"
        "Wallpaper",
        headers={
            "Authorization": f"Bearer {get_oauth_token()}",
            "Content-Type": "application/xml",
        },
        data=body,
        timeout=60,
    )
    if resp.status_code >= 400:
        print(f"Wallpaper command refused: HTTP {resp.status_code}")
        sys.exit(24)


def main():
    event_data = json.loads(sys.argv[1])
    event = event_data.get("event") or {}
    jss_id = event.get("jssID")
    udid = event.get("udid")
    if not isinstance(jss_id, int) or not udid:
        print("Payload did not name a device id and UDID.")
        sys.exit(12)

    device = fetch_verified_device(jss_id, udid)
    basename, role = find_role(device)
    image = make_image(ASSETS_DIR, basename, role, device, "", "/tmp")
    try:
        set_wallpaper(image, jss_id)
    finally:
        if os.path.exists(image):
            os.remove(image)
    print(f"Device {jss_id}: wallpaper set ({basename}.png).")


if __name__ == "__main__":
    main()
