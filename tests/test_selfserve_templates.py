"""Self-serve templates: catalog display and the enable path."""

import json
import os

import pytest

from bin import data_store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO_ROOT, "data", "workflows", "workflow_config.json")


def _catalog():
    with open(CONFIG, "r", encoding="utf-8") as handle:
        return json.load(handle)


SELFSERVE = [wf for wf in _catalog() if wf.get("trigger_type") == "selfserve"]
SLUGS = [wf["slug"] for wf in SELFSERVE]


@pytest.fixture(params=SELFSERVE, ids=SLUGS)
def selfserve_workflow(request):
    return request.param


def test_at_least_the_rts_template_is_selfserve():
    assert "self-serve-return-to-service" in SLUGS


def test_selfserve_entry_declares_page_strings_and_family(
    selfserve_workflow,
):
    block = selfserve_workflow["selfserve"]
    assert block["device_family"] in ("mobile", "computer")
    for key in (
        "page_title",
        "page_description",
        "confirm_label",
        "success_message",
        "failure_message",
    ):
        assert block[key].strip(), f"{selfserve_workflow['slug']} {key}"
    assert selfserve_workflow.get("trigger_event") is None


def test_catalog_and_detail_label_selfserve_trigger(
    logged_in_client, jawa_env, selfserve_workflow
):
    slug = selfserve_workflow["slug"]
    for path in ("/templates", f"/templates/{slug}", f"/templates/{slug}/enable"):
        body = logged_in_client.get(path).get_data(as_text=True)
        assert "Self-serve (web clip)" in body, path
        assert ">None<" not in body


def test_enable_form_shows_page_fields_not_jamf_event(
    logged_in_client, jawa_env, selfserve_workflow
):
    body = logged_in_client.get(
        f"/templates/{selfserve_workflow['slug']}/enable"
    ).get_data(as_text=True)
    assert 'name="page_title"' in body
    assert 'name="create_profile"' in body
    assert 'name="event"' not in body
    assert "Webhook Authentication" not in body


def test_enabling_creates_a_selfserve_record_that_fires(
    logged_in_client, jawa_env, enable_form, monkeypatch
):
    import io
    import subprocess

    calls = []

    class P:
        def __init__(self, args, stdout=None, stderr=None):
            calls.append(args)
            self.stdout = io.BytesIO(b"ok\n")

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", P)

    form = enable_form(
        "self-serve-return-to-service", webhook_name="reset-ipad"
    )
    resp = logged_in_client.post(
        "/templates/self-serve-return-to-service/enable", data=form
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)
    entry = data_store.get_webhook_by_name("reset-ipad")
    assert entry is not None
    assert entry["tag"] == "selfserve"
    assert entry["device_family"] == "mobile"
    assert entry["page_title"]  # defaults came from the catalog
    assert entry["profile_status"] == "skipped"  # no checkbox in form
    assert os.path.isfile(entry["script"])
    with open(entry["script"], encoding="utf-8") as handle:
        source = handle.read()
    assert "__JAWA_" not in source
    assert form["wifi_ssid"] in source

    resp = logged_in_client.post(
        "/selfserve/reset-ipad",
        data={
            "token": entry["token"],
            "id": "7",
            "udid": "UDID-7",
            "DEVICENAME": "iPad-7",
        },
    )
    assert resp.status_code == 200
    assert calls and calls[0][0] == entry["script"]
    payload = json.loads(calls[0][1])
    assert payload["event"]["jssID"] == 7


def test_enable_overrides_page_strings_from_the_form(
    logged_in_client, jawa_env, enable_form
):
    form = enable_form(
        "self-serve-return-to-service",
        webhook_name="reset-2",
        page_title="Custom {DEVICENAME}",
    )
    logged_in_client.post(
        "/templates/self-serve-return-to-service/enable", data=form
    )
    assert (
        data_store.get_webhook_by_name("reset-2")["page_title"]
        == "Custom {DEVICENAME}"
    )


ASSETS = os.path.join(REPO_ROOT, "data", "workflows", "assets", "brander")


def test_brander_template_and_assets_ship():
    assert "self-serve-brander" in SLUGS
    for name in (
        "lockscreen-template.png",
        "null.png",
        "brandlogo.png",
        "nursing.png",
        "patient.png",
        "pharmacist.png",
        "technician.png",
        "transport.png",
        "videoconferencing.png",
        "README.md",
    ):
        assert os.path.isfile(os.path.join(ASSETS, name)), name
    assert not os.path.exists(os.path.join(ASSETS, "SF-Pro.ttf"))


def test_brander_requirements_are_declared():
    path = os.path.join(REPO_ROOT, "requirements.txt")
    with open(path, encoding="utf-8") as handle:
        reqs = handle.read().lower()
    assert "pillow" in reqs
    assert "qrcode" in reqs


def test_brander_script_generates_an_image_without_a_font_file(tmp_path):
    """The image pipeline must work with Pillow's built-in font so the
    template ships with no proprietary font."""
    import importlib.util

    path = os.path.join(
        REPO_ROOT, "data", "workflows", "scripts", "self_serve_brander.py"
    )
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    source = (
        source.replace('"__JAWA_EA_ID__"', "0")
        .replace("__JAWA_EA_ID__", "0")
        .replace("__JAWA_WALLPAPER_SETTING__", "3")
        # font_path is a required field at enable time, so the deployed
        # script never carries the bare token; "none" is the sentinel
        # value load_font() treats as the built-in font.
        .replace('"__JAWA_FONT_PATH__"', '"none"')
    )
    spec = importlib.util.spec_from_loader("ss_brander", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, path, "exec"), module.__dict__)
    device = {
        "general": {
            "id": 42,
            "device_name": "iPad-042",
            "serial_number": "DMPX1",
            "model": "iPad",
            "os_type": "iPadOS",
            "os_version": "18.0",
        },
        "location": {
            "building": "Main",
            "department": "Nursing",
            "room": "3A",
            "username": "nurse",
        },
    }
    out = module.make_image(
        ASSETS, "lockscreen-template", "Nursing", device, "", str(tmp_path)
    )
    assert os.path.isfile(out)
    from PIL import Image

    with Image.open(out) as img:
        assert img.size[0] > 100
