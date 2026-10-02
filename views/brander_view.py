"""Brander template store in the console (M3-4): list, upload, activate.

Scoped to the Jamf Pro tenant of the logged-in session, and audited with
the logged-in admin's name. Packages are .brander.json files exported
from EWOK; see bin/brander_store.py for the format and the checks.
"""

import base64
import io
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from bin import brander_settings, brander_store, brander_values, logger
from bin.auth import login_required
from bin.data_store import get_webhooks_by_tag

logthis = logger.setup_child_logger("jawa", "brander_view")

blueprint = Blueprint("brander", __name__)

MAX_UPLOAD_BYTES = brander_store.MAX_PACKAGE_BYTES


@blueprint.app_template_filter("timestamp_to_text")
def timestamp_to_text(seconds: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(seconds))


def used_by() -> dict:
    """slug -> names of the self-serve Branders whose template is store:<slug>."""
    found: dict = {}
    for entry in get_webhooks_by_tag("selfserve"):
        script = entry.get("script", "")
        if brander_settings.is_brander_script(script):
            path = str(brander_settings.read_settings(script).get("template_path") or "")
            if path.startswith("store:"):
                slug = brander_store.slugify(path[len("store:"):])
                found.setdefault(slug, []).append(entry.get("name", ""))
    return found


def _page(status=200, **context) -> tuple:
    templates = brander_store.list_templates(session.get("url", ""))
    return render_template("brander/templates.html", templates=templates,
                           used_by=used_by(), **context), status


@blueprint.route("/brander/templates", methods=["GET"])
@login_required
def templates() -> Union[Response, str, tuple]:
    return _page()


@blueprint.route("/brander/templates", methods=["POST"])
@login_required
def upload() -> Union[Response, str, tuple]:
    upload_file = request.files.get("package")
    if not upload_file or not upload_file.filename:
        return _page(400, problems=["Choose a .brander.json file to upload."])
    raw = upload_file.read(MAX_UPLOAD_BYTES + 1)
    try:
        result = brander_store.save_version(
            session.get("url", ""), raw, session.get("username", "unknown")
        )
    except brander_store.StoreError as err:
        logthis.info(
            f"{session.get('username')} uploaded a Brander template that "
            f"was refused: {err.problems[:3]}"
        )
        return _page(400, problems=err.problems, filename=upload_file.filename)
    logthis.info(
        f"{session.get('username')} uploaded Brander template "
        f"{result['slug']} v{result['version']} (sha256 {result['sha256'][:12]})."
    )
    names = {t["slug"]: t["name"] for t in brander_store.list_templates(session.get("url", ""))}
    return _page(uploaded=dict(result, name=names.get(result["slug"], result["slug"])))


@blueprint.route("/brander/templates/<slug>/activate", methods=["POST"])
@login_required
def activate(slug: str) -> Union[Response, str, tuple]:
    text = (request.form.get("version") or "").strip()
    if not text.isdigit():
        return _page(400, problems=["Choose a version to activate."])
    try:
        brander_store.activate(
            session.get("url", ""), slug, int(text), session.get("username", "unknown")
        )
    except brander_store.StoreError as err:
        return _page(400, problems=err.problems)
    logthis.info(f"{session.get('username')} activated Brander template {slug} v{text}.")
    return redirect(url_for("brander.templates"))


# --- Preview a template on a real device, sending nothing (M3-5) ----------

_base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRANDER_ASSETS_DIR = os.path.join(_base_dir, "data", "workflows", "assets", "brander")
USER_AGENT = "JAWA Brander preview"


def _jamf_get(path: str) -> requests.Response:
    """A read-only Classic API call with the admin's session token."""
    from views._type_handlers.selfserve_handler import _fresh_token

    session_data = {"token": session.get("token"), "expires": session.get("expires")}
    _fresh_token(session_data)
    return requests.get(
        f"{session.get('url', '').rstrip('/')}/JSSResource/{path}",
        headers={"Authorization": f"Bearer {session_data['token']}",
                 "Accept": "application/json", "User-Agent": USER_AGENT},
        timeout=30,
    )


def preview_choices() -> List[str]:
    stored = [f"store:{t['slug']}" for t in brander_store.list_templates(session.get("url", ""))]
    shipped = sorted(
        name for name in os.listdir(BRANDER_ASSETS_DIR) if name.endswith(".json")
    ) if os.path.isdir(BRANDER_ASSETS_DIR) else []
    return stored + shipped


def _template_file(choice: str, device: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """(path of the template to render, note on how it was chosen)."""
    if choice.startswith("store:"):
        path = brander_store.active_template_path(session.get("url", ""), choice[len("store:"):])
        return path, choice
    path = os.path.join(BRANDER_ASSETS_DIR, choice)
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict) and "templates" in data and "schema_version" not in data:
        entry = brander_values.set_entry(data, device)
        if not entry or os.path.basename(entry) != entry:
            return None, f"{choice} has no entry for this device"
        return os.path.join(BRANDER_ASSETS_DIR, entry), f"{choice} picked {entry}"
    return path, choice


def _folder_assets(folders: List[str]):
    def resolve(asset_id: str) -> bytes:
        wallrender = brander_store._wallrender()
        if wallrender.schema.ASSET_ID.fullmatch(asset_id):
            for folder in folders:
                root = os.path.realpath(folder)
                for ext in ("png", "jpg", "jpeg"):
                    path = os.path.realpath(os.path.join(root, f"{asset_id}.{ext}"))
                    if path.startswith(root + os.sep) and os.path.isfile(path):
                        with open(path, "rb") as handle:
                            return handle.read()
        raise KeyError(asset_id)

    return resolve


def _preview_page(status=200, **context) -> tuple:
    return render_template("brander/preview.html", choices=preview_choices(),
                           **context), status


@blueprint.route("/brander/preview", methods=["GET"])
@login_required
def preview_form() -> Union[Response, str, tuple]:
    return _preview_page()


@blueprint.route("/brander/preview", methods=["POST"])
@login_required
def preview() -> Union[Response, str, tuple]:
    choice = (request.form.get("template") or "").strip()
    jss_text = (request.form.get("jss_id") or "").strip()
    allow = request.form.get("allow_person_fields") == "on"
    form = {"template": choice, "jss_id": jss_text, "allow": allow}
    if choice not in preview_choices():
        return _preview_page(400, form=form, problems=["Choose a template from the list."])
    if not jss_text.isdigit():
        return _preview_page(400, form=form, problems=["Enter the device's Jamf Pro ID (a number)."])
    resp = _jamf_get(f"mobiledevices/id/{int(jss_text)}")
    if resp.status_code == 404:
        return _preview_page(404, form=form, problems=[f"No mobile device with Jamf ID {jss_text}."])
    if resp.status_code >= 400:
        return _preview_page(502, form=form, problems=[f"Jamf Pro refused the lookup (HTTP {resp.status_code})."])
    device = resp.json().get("mobile_device", {})
    path, chosen = _template_file(choice, device)
    if not path or not os.path.isfile(path):
        return _preview_page(400, form=form, problems=[f"No template to render: {chosen}."])
    with open(path, encoding="utf-8") as handle:
        template = json.load(handle)
    wallrender = brander_store._wallrender()
    values = brander_values.build_values(device, template, allow)
    assets = _folder_assets([os.path.dirname(path), BRANDER_ASSETS_DIR])
    try:
        image = wallrender.render(template, values, assets)
        warnings = wallrender.lint(template, values, assets)
    except wallrender.TemplateError as err:
        return _preview_page(422, form=form, problems=err.problems)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    general = device.get("general", {})
    logthis.info(f"{session.get('username')} previewed {chosen} for device {jss_text} (nothing sent).")
    return _preview_page(
        form=form, chosen=chosen, warnings=warnings,
        image=base64.b64encode(buffer.getvalue()).decode("ascii"),
        size=image.size,
        device={"name": general.get("device_name", ""), "model": general.get("model_identifier", ""),
                "supervised": general.get("supervised"), "os_type": general.get("os_type", "")},
    )


@blueprint.route("/brander/ea-names.json")
@login_required
def ea_names() -> Union[Response, tuple]:
    """The tenant's mobile device extension attribute names (names only,
    never values) for EWOK's role attribute picker."""
    resp = _jamf_get("mobiledeviceextensionattributes")
    if resp.status_code >= 400:
        return {"error": f"Jamf Pro refused the lookup (HTTP {resp.status_code})."}, 502
    items = resp.json().get("mobile_device_extension_attributes", [])
    names = sorted({str(item.get("name", "")) for item in items if item.get("name")})
    payload = json.dumps({"kind": "jamf-ea-names", "format": 1, "names": names}, indent=1)
    return send_file(io.BytesIO(payload.encode()), mimetype="application/json",
                     as_attachment=True, download_name="jamf-ea-names.json")
