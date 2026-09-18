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

import secrets
from typing import Any, Dict, List, Mapping, Optional, Tuple

from bin import logger
from bin.data_store import (
    get_jawa_address,
    retire_script,
    save_all_webhooks,
    save_script,
)
from views._type_handlers.base import AutomationError, AutomationHandler
from views._type_handlers.jamf_handler import validate_webhook_name

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
        url = service_url(jawa_address, name, entry["token"])
        logthis.info(
            f"{session_data.get('username')} created self-serve "
            f"automation {name}."
        )
        return {
            "entry": entry,
            "success_msg": "New self-serve automation created:",
            "new_here": name,
            "new_link": f"/automations/selfserve/{name}",
            "extra_notice": "Web clip URL for the configuration profile:",
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
        save_all_webhooks(all_items)
        logthis.info(
            f"{session_data.get('username')} edited self-serve "
            f"automation {existing['name']}."
        )
        return {
            "success_msg": (
                f"Edited self-serve automation {existing['name']}."
            )
        }

    def process_delete(
        self, automation: Dict, session_data: Dict
    ) -> Optional[str]:
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
