# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#
# Copyright (c) 2022 Jamf.  All rights reserved.
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

"""Device-facing receiver for self-serve automations.

A self-serve automation is triggered by a person tapping a web clip on
the managed device itself. The web clip URL carries a per-service
token plus Jamf Pro payload variables (``$JSSID``, ``$UDID`` and any
display values the admin adds). GET renders a confirmation page from
that query string; only the page's POST spawns the script. See the
Self-serve section of README.md for the URL and payload contract.
"""

import hmac
import re
import time
from typing import Any, Dict, Mapping, Optional, Tuple

from flask import Blueprint, render_template, request

from bin import data_store, logger

logthis = logger.setup_child_logger("jawa", "selfserve_receiver")

blueprint = Blueprint(
    "selfserve_receiver", __name__, template_folder="templates"
)

RESERVED_PARAMS = ("token", "id", "udid")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")


class SelfServeRequestError(Exception):
    """A device request JAWA refuses before anything runs."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def fill_placeholders(text: str, params: Mapping[str, str]) -> str:
    """Substitute ``{NAME}`` with the query-string value of NAME.

    Unknown names render empty. The result is raw text: Jinja escapes
    it at render time, so a caller-supplied value can never become
    markup.
    """
    return _PLACEHOLDER_RE.sub(
        lambda m: str(params.get(m.group(1), "")), text or ""
    )


def parse_device_request(
    fields: Mapping[str, str],
) -> Tuple[int, str, Dict[str, str]]:
    """Split a query string / form into (jss_id, udid, params).

    ``id`` and ``udid`` are required because the script's UDID
    cross-check (ADR-0012) needs both. Everything else that is not
    reserved passes through untouched under ``params``.
    """
    raw_id = (fields.get("id") or "").strip()
    udid = (fields.get("udid") or "").strip()
    if not raw_id or not udid:
        raise SelfServeRequestError(
            400, "This link is missing the device id or UDID."
        )
    if not raw_id.isdigit():
        raise SelfServeRequestError(400, "The device id is not a number.")
    params = {
        key: value
        for key, value in fields.items()
        if key not in RESERVED_PARAMS
    }
    return int(raw_id), udid, params


def token_matches(
    service: Dict[str, Any], presented: Optional[str]
) -> bool:
    expected = str(service.get("token") or "")
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, str(presented))


def build_selfserve_payload(
    service: Dict[str, Any],
    jss_id: int,
    udid: str,
    params: Dict[str, str],
    remote_address: str,
) -> Dict[str, Any]:
    """The argv[1] JSON a self-serve script receives.

    Shaped like a Jamf Pro event so scripts written for
    MobileDeviceEnrolled-style payloads port with a one-line change.
    """
    return {
        "webhook": {
            "webhookEvent": "SelfServe",
            "name": service.get("name", ""),
            "eventTimestamp": int(time.time() * 1000),
        },
        "event": {
            "jssID": jss_id,
            "udid": udid,
            "deviceType": service.get("device_family", "mobile"),
            "params": params,
            "remoteAddress": remote_address,
        },
    }


def load_service(name: str) -> Optional[Dict[str, Any]]:
    entry = data_store.get_webhook_by_name(name)
    if not entry or entry.get("tag") != "selfserve":
        return None
    return entry


def _remote_address() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def _authorize(service_name: str, fields: Mapping[str, str]):
    """Resolve the service and check its token, or raise 401.

    The 401 body never carries the name: it is caller-controlled path
    input and the log line is where an operator looks anyway.
    """
    service = load_service(service_name)
    if not service or not token_matches(service, fields.get("token")):
        logthis.warning(
            f"401 - self-serve request refused for /selfserve/"
            f"{service_name} (unknown service or bad token)."
        )
        raise SelfServeRequestError(
            401, "Unauthorized - this link is not valid."
        )
    return service


def _page_strings(service: Dict[str, Any], params: Dict[str, str]):
    return {
        key: fill_placeholders(service.get(key, ""), params)
        for key in (
            "page_title",
            "page_description",
            "confirm_label",
            "success_message",
            "failure_message",
        )
    }


@blueprint.route("/selfserve/<service_name>", methods=["GET"])
def service_page(service_name: str):
    logthis.info(f"Incoming GET at /selfserve/{service_name} ...")
    try:
        service = _authorize(service_name, request.args)
        jss_id, udid, params = parse_device_request(request.args)
    except SelfServeRequestError as err:
        return err.message, err.status
    return render_template(
        "selfserve/confirm.html",
        service_name=service["name"],
        strings=_page_strings(service, params),
        token=request.args.get("token", ""),
        jss_id=jss_id,
        udid=udid,
        params=params,
    )
