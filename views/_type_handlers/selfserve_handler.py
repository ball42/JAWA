# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#
# Copyright (c) 2026 Jamf.  All rights reserved.
#
#       Redistribution and use in source and binary forms, with or without
#       modification, are permitted provided that the following conditions are met:
#               * Redistributions of source code must retain the above copyright
#                 notice, this list of conditions and the following disclaimer.
#               * Redistributions in binary form must reproduce the above copyright
#                 notice, this list of conditions and the following disclaimer in the
#                 documentation and/or other materials provided with the distribution.
#               * Neither the name of the Jamf nor the names of its contributors may be
#                 used to endorse or promote products derived from this software without
#                 specific prior written permission.
#
#       THIS SOFTWARE IS PROVIDED BY JAMF SOFTWARE, LLC "AS IS" AND ANY
#       EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
#       WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#       DISCLAIMED. IN NO EVENT SHALL JAMF SOFTWARE, LLC BE LIABLE FOR ANY
#       DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
#       (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
#       LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
#       ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#       (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#       SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

"""Self-serve automations: triggered by a person tapping a web clip on
the managed device. See README.md, "Self-serve automations", and the
device-facing receiver in webhook/selfserve_receiver.py.
"""

import plistlib
import re
import secrets
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

import requests

from bin import logger
from bin.data_store import (
    get_jawa_address,
    retire_script,
    save_all_webhooks,
    save_script,
)
from bin.tokens import get_token, validate_token
from views._type_handlers.base import AutomationError, AutomationHandler
from views._type_handlers.jamf_handler import (
    USER_AGENT_STRING,
    VERIFY_SSL,
    XML,
    validate_webhook_name,
    xml_escape,
)

logthis = logger.setup_child_logger("jawa", "selfserve_handler")

PAGE_FIELDS = (
    "page_title",
    "page_description",
    "confirm_label",
    "success_message",
    "failure_message",
)
PAGE_FIELD_LABELS = {
    "page_title": "Page title",
    "page_description": "Page description",
    "confirm_label": "Confirm button label",
    "success_message": "Success message",
    "failure_message": "Failure message",
}
DEVICE_FAMILIES = ("mobile", "computer")

# Display variables Jamf Pro fills into the web clip URL. The admin can
# add more in Jamf Pro; these two give the default page strings
# something to show.
DISPLAY_VARIABLES = ("DEVICENAME", "SERIALNUMBER")


def mint_token() -> str:
    return secrets.token_urlsafe(32)


def service_url(jawa_address: str, name: str, token: str) -> str:
    """The URL to paste into the Web Clip payload.

    ``$JSSID`` etc. are Jamf Pro payload variables, substituted per
    device when the profile installs.
    """
    display = "&".join(f"{v}=${v}" for v in DISPLAY_VARIABLES)
    return (
        f"{jawa_address.rstrip('/')}/selfserve/{name}"
        f"?token={token}&id=$JSSID&udid=$UDID&{display}"
    )


_PLACEHOLDER_RE = re.compile(r"\s*\{[A-Za-z0-9_]+\}\s*")
_PRE_PUNCT_RE = re.compile(r"\s+([?!.,:;])")
PROFILE_PREFIX = "JAWA Self-Serve: "
PROFILE_ENDPOINT = "/JSSResource/mobiledeviceconfigurationprofiles"


def _label(entry: Dict[str, Any]) -> str:
    text = _PLACEHOLDER_RE.sub(" ", entry.get("page_title", ""))
    text = _PRE_PUNCT_RE.sub(r"\1", text)
    text = " ".join(text.split())
    return text or entry.get("name", "Self-Serve")


def _uuid_for(identifier: str) -> str:
    """A deterministic PayloadUUID so re-building the same profile
    (e.g. on edit) does not needlessly change every UUID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identifier)).upper()


def build_webclip_plist(entry: Dict[str, Any], url: str) -> str:
    name = entry["name"]
    base_id = f"com.jamf.jawa.selfserve.{name}"
    payload = {
        "PayloadContent": [
            {
                "FullScreen": True,
                "IsRemovable": False,
                "Label": _label(entry),
                "PayloadDescription": (
                    "Configures a JAWA self-serve web clip"
                ),
                "PayloadDisplayName": "Web Clip",
                "PayloadIdentifier": f"{base_id}.webclip",
                "PayloadType": "com.apple.webClip.managed",
                "PayloadUUID": _uuid_for(f"{base_id}.webclip"),
                "PayloadVersion": 1,
                "Precomposed": True,
                "URL": url,
            }
        ],
        "PayloadDisplayName": PROFILE_PREFIX + _label(entry),
        "PayloadIdentifier": base_id,
        "PayloadOrganization": "JAWA",
        "PayloadRemovalDisallowed": True,
        "PayloadScope": "System",
        "PayloadType": "Configuration",
        "PayloadUUID": _uuid_for(base_id),
        "PayloadVersion": 1,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode("utf-8")


def build_profile_xml(entry: Dict[str, Any], url: str) -> str:
    """Classic API body. The plist rides inside <payloads> as escaped
    text, which is how Jamf Pro's Classic API takes custom profiles."""
    name = xml_escape(PROFILE_PREFIX + _label(entry))
    description = xml_escape(
        f"Created by JAWA for the self-serve automation "
        f"\"{entry['name']}\". Scope it to the devices that should show "
        "the web clip."
    )
    plist = xml_escape(build_webclip_plist(entry, url))
    return (
        "<mobile_device_configuration_profile><general>"
        f"<name>{name}</name><description>{description}</description>"
        "<distribution_method>Install Automatically</distribution_method>"
        "<deployment_method>Install Automatically</deployment_method>"
        "<redeploy_on_update>All</redeploy_on_update>"
        f"<payloads>{plist}</payloads></general>"
        "<scope><all_mobile_devices>false</all_mobile_devices></scope>"
        "</mobile_device_configuration_profile>"
    )


def _headers(session_data: Dict[str, Any]) -> Dict[str, str]:
    return {
        "Content-Type": XML,
        "Authorization": f"Bearer {session_data.get('token')}",
        "User-Agent": USER_AGENT_STRING,
    }


def _fresh_token(session_data: Dict[str, Any]) -> None:
    if not validate_token(session_data.get("expires")):
        get_token()


def create_webclip_profile(
    entry: Dict[str, Any], url: str, session_data: Dict[str, Any]
) -> Optional[str]:
    """POST the profile; return its Jamf id, or None on any failure.

    Never raises: the service is still useful without the profile (the
    detail page shows the URL for manual creation), so a Jamf error
    degrades to profile_status "failed" rather than aborting create.
    """
    full_url = f"{session_data['url']}{PROFILE_ENDPOINT}/id/0"
    try:
        _fresh_token(session_data)
        resp = requests.post(
            full_url,
            headers=_headers(session_data),
            data=build_profile_xml(entry, url),
            verify=VERIFY_SSL,
            timeout=30,
        )
    except Exception as err:
        logthis.error(
            f"Could not create the web clip profile for "
            f"{entry['name']}: {err}"
        )
        return None
    if resp.status_code >= 400:
        logthis.error(
            f"Jamf Pro refused the web clip profile for {entry['name']}: "
            f"HTTP {resp.status_code} - {resp.text}"
        )
        return None
    match = re.search(r"<id>(\d+)</id>", resp.text or "")
    if not match:
        logthis.error(
            f"Jamf Pro answered {resp.status_code} for the web clip "
            f"profile of {entry['name']} but returned no id."
        )
        return None
    return match.group(1)


def update_webclip_profile(
    entry: Dict[str, Any], url: str, session_data: Dict[str, Any]
) -> bool:
    if not entry.get("jamf_id"):
        return False
    full_url = (
        f"{session_data['url']}{PROFILE_ENDPOINT}/id/{entry['jamf_id']}"
    )
    try:
        _fresh_token(session_data)
        resp = requests.put(
            full_url,
            headers=_headers(session_data),
            data=build_profile_xml(entry, url),
            verify=VERIFY_SSL,
            timeout=30,
        )
    except Exception as err:
        logthis.error(f"Could not update profile {entry['jamf_id']}: {err}")
        return False
    if resp.status_code >= 400:
        logthis.error(
            f"Jamf Pro refused the profile update for {entry['name']}: "
            f"HTTP {resp.status_code}"
        )
        return False
    return True


def retire_webclip_profile(
    entry: Dict[str, Any], session_data: Dict[str, Any]
) -> None:
    """Rename and unscope rather than delete, mirroring webhook delete."""
    if not entry.get("jamf_id"):
        return
    name = xml_escape(
        f"{PROFILE_PREFIX}{_label(entry)}.old.{time.time()}"
    )
    body = (
        "<mobile_device_configuration_profile>"
        f"<general><name>{name}</name></general>"
        "<scope><all_mobile_devices>false</all_mobile_devices>"
        "<mobile_devices/><mobile_device_groups/><buildings/>"
        "<departments/></scope>"
        "</mobile_device_configuration_profile>"
    )
    full_url = (
        f"{session_data['url']}{PROFILE_ENDPOINT}/id/{entry['jamf_id']}"
    )
    try:
        _fresh_token(session_data)
        resp = requests.put(
            full_url,
            headers=_headers(session_data),
            data=body,
            verify=VERIFY_SSL,
            timeout=30,
        )
        if resp.status_code >= 400:
            logthis.error(
                f"Error retiring profile {entry['jamf_id']}: "
                f"HTTP {resp.status_code}"
            )
    except Exception as err:
        logthis.error(f"Failed to retire profile {entry['jamf_id']}: {err}")


def register_profile(
    entry: Dict[str, Any], session_data: Dict[str, Any], wanted: bool
) -> None:
    """Fill jamf_id/profile_status on a freshly built entry.

    Web Clips are a mobile device management profile payload; Jamf Pro
    has no equivalent for computers, so a computer-family service is
    always skipped regardless of the checkbox.
    """
    if not wanted or entry.get("device_family") != "mobile":
        entry["jamf_id"] = None
        entry["profile_status"] = "skipped"
        return
    url = service_url(
        get_jawa_address() or "", entry["name"], entry["token"]
    )
    jamf_id = create_webclip_profile(entry, url, session_data)
    entry["jamf_id"] = jamf_id
    entry["profile_status"] = "created" if jamf_id else "failed"


def _require_page_strings(form: Mapping[str, Any]) -> Dict[str, str]:
    strings = {}
    missing = []
    for key in PAGE_FIELDS:
        value = (form.get(key) or "").strip()
        if not value:
            missing.append(PAGE_FIELD_LABELS[key])
        strings[key] = value
    if missing:
        raise AutomationError(
            "Missing page text",
            "Fill in every page field. Missing: " + ", ".join(missing),
        )
    return strings


def _require_device_family(form: Mapping[str, Any]) -> str:
    family = (form.get("device_family") or "").strip()
    if family not in DEVICE_FAMILIES:
        raise AutomationError(
            "Error", "Device family must be mobile or computer."
        )
    return family


def build_service_entry(
    name: str,
    script_path: str,
    form: Mapping[str, Any],
    session_data: Dict[str, Any],
    description: str = "",
) -> Dict[str, Any]:
    """The full self-serve record, validated, with a fresh token and no
    Jamf Pro profile yet (Task 4 fills jamf_id/profile_status)."""
    validate_webhook_name(name)
    family = _require_device_family(form)
    strings = _require_page_strings(form)
    entry = {
        "name": name,
        "tag": "selfserve",
        "url": str(session_data.get("url", "")),
        "jawa_admin": str(session_data.get("username", "")),
        "script": script_path,
        "description": description or form.get("description", ""),
        "token": mint_token(),
        "device_family": family,
        "jamf_id": None,
        "profile_status": "skipped",
        "webhook_username": "null",
        "webhook_password": "null",
        "api_key": "null",
    }
    entry.update(strings)
    return entry


class SelfServeHandler(AutomationHandler):
    tag = "selfserve"
    display_name = "Self-Serve"
    badge_class = "badge-provisioning"
    icon = "webhook.png"
    supports_edit = True
    supports_auth = False

    def get_create_context(self, session_data: Dict) -> Dict[str, Any]:
        return {
            "device_families": DEVICE_FAMILIES,
            "page_fields": PAGE_FIELDS,
        }

    def process_create(
        self, form: Any, files: Any, session_data: Dict
    ) -> Dict[str, Any]:
        name = (form.get("service_name") or "").strip()
        validate_webhook_name(name)
        jawa_address = get_jawa_address()
        if not jawa_address:
            raise AutomationError(
                "Setup Required",
                "Configure your JAWA address before creating a "
                "self-serve automation.",
                link="/setup",
                link_text="Go to Setup",
            )
        new_file = files.get("new_file")
        if not new_file or not new_file.filename:
            raise AutomationError("Error", "A script file is required.")
        # Validate the form before touching disk so a bad field does not
        # leave an orphaned script behind.
        _require_device_family(form)
        _require_page_strings(form)
        script_path = save_script(new_file, name)
        entry = build_service_entry(name, script_path, form, session_data)
        wanted_profile = form.get("create_profile") == "on"
        register_profile(entry, session_data, wanted_profile)
        url = service_url(jawa_address, name, entry["token"])
        logthis.info(
            f"{session_data.get('username')} created self-serve "
            f"automation {name}."
        )
        extra_notice = "Web clip URL for the configuration profile:"
        if wanted_profile and entry["device_family"] != "mobile":
            extra_notice = (
                "Web clip profiles are created automatically for "
                "mobile services only; create the profile by hand "
                "with this URL:"
            )
        elif entry["profile_status"] == "failed":
            extra_notice = (
                "Jamf Pro did not accept the web clip profile; create "
                "it by hand with this URL:"
            )
        return {
            "entry": entry,
            "success_msg": "New self-serve automation created:",
            "new_here": name,
            "new_link": f"/automations/selfserve/{name}",
            "extra_notice": extra_notice,
            "custom_header": {"URL": url},
        }

    def process_edit(
        self,
        form: Any,
        files: Any,
        session_data: Dict,
        existing: Dict,
        all_items: Any,
    ) -> Dict[str, Any]:
        strings = _require_page_strings(form)
        family = _require_device_family(form)
        existing.update(strings)
        existing["device_family"] = family
        if form.get("description"):
            existing["description"] = form.get("description")
        if files.get("new_file") and files["new_file"].filename:
            existing["script"] = save_script(
                files["new_file"], existing["name"]
            )

        extra_notice = None
        custom_header = None
        if existing.get("jamf_id"):
            profile_url = service_url(
                get_jawa_address() or "",
                existing["name"],
                existing["token"],
            )
            if update_webclip_profile(existing, profile_url, session_data):
                existing["profile_status"] = "created"
            else:
                existing["profile_status"] = "failed"
                extra_notice = (
                    "Jamf Pro did not accept the profile update; "
                    "update it by hand with this URL:"
                )
                custom_header = {"URL": profile_url}

        save_all_webhooks(all_items)
        logthis.info(
            f"{session_data.get('username')} edited self-serve "
            f"automation {existing['name']}."
        )
        result = {
            "success_msg": (
                f"Edited self-serve automation {existing['name']}."
            )
        }
        if extra_notice:
            result["extra_notice"] = extra_notice
            result["custom_header"] = custom_header
        return result

    def process_delete(
        self, automation: Dict, session_data: Dict
    ) -> Optional[str]:
        retire_webclip_profile(automation, session_data)
        retire_script(automation.get("script", ""))
        return None

    def get_detail_fields(self, automation: Dict) -> List[Tuple[str, str]]:
        jawa_address = get_jawa_address() or ""
        fields = [
            ("Service name", automation.get("name", "")),
            (
                "Web clip URL",
                service_url(
                    jawa_address,
                    automation.get("name", ""),
                    automation.get("token", ""),
                ),
            ),
            ("Device family", automation.get("device_family", "")),
            ("Script path", automation.get("script", "")),
            ("Description", automation.get("description", "")),
            ("Created by", automation.get("jawa_admin", "")),
            ("Jamf Pro profile", automation.get("profile_status", "")),
        ]
        for key in PAGE_FIELDS:
            fields.append((PAGE_FIELD_LABELS[key], automation.get(key, "")))
        if automation.get("jamf_id"):
            fields.append(
                ("Jamf Pro profile ID", str(automation["jamf_id"]))
            )
        return fields
