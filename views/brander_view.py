"""Brander template store in the console (M3-4): list, upload, activate.

Scoped to the Jamf Pro tenant of the logged-in session, and audited with
the logged-in admin's name. Packages are .brander.json files exported
from EWOK; see bin/brander_store.py for the format and the checks.
"""

import time
from typing import Union

from flask import Blueprint, Response, redirect, render_template, request, session, url_for

from bin import brander_store, logger
from bin.auth import login_required

logthis = logger.setup_child_logger("jawa", "brander_view")

blueprint = Blueprint("brander", __name__)

MAX_UPLOAD_BYTES = brander_store.MAX_PACKAGE_BYTES


@blueprint.app_template_filter("timestamp_to_text")
def timestamp_to_text(seconds: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(seconds))


def _page(status=200, **context) -> tuple:
    templates = brander_store.list_templates(session.get("url", ""))
    return render_template("brander/templates.html", templates=templates, **context), status


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
