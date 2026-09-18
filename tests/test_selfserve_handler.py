"""Console side of self-serve automations: the type handler."""

import io

import pytest

from bin import data_store
from views._type_handlers import get_handler
from views._type_handlers import selfserve_handler as ssh


def _script_upload():
    return (io.BytesIO(b"#!/bin/bash\necho hi\n"), "reset.sh")


def _create_form(**overrides):
    form = {
        "service_name": "reset-ipad",
        "description": "Loaner reset",
        "device_family": "mobile",
        "page_title": "Reset {DEVICENAME}?",
        "page_description": "It will erase itself.",
        "confirm_label": "Erase and reset",
        "success_message": "Done.",
        "failure_message": "Failed. Tell IT.",
        "new_file": _script_upload(),
    }
    form.update(overrides)
    return form


def test_handler_is_registered():
    handler = get_handler("selfserve")
    assert handler is not None
    assert handler.tag == "selfserve"
    assert handler.display_name == "Self-Serve"


def test_service_url_carries_token_and_reserved_variables():
    url = ssh.service_url("https://jawa.example.test", "reset-ipad", "T")
    assert url.startswith("https://jawa.example.test/selfserve/reset-ipad?")
    assert "token=T" in url
    assert "id=$JSSID" in url
    assert "udid=$UDID" in url
    assert "DEVICENAME=$DEVICENAME" in url


def test_mint_token_is_long_and_unique():
    a, b = ssh.mint_token(), ssh.mint_token()
    assert a != b
    assert len(a) >= 32


def test_create_persists_the_full_record(logged_in_client, jawa_env):
    resp = logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302 and "/success" in resp.headers["Location"]
    entry = data_store.get_webhook_by_name("reset-ipad")
    assert entry is not None
    assert entry["tag"] == "selfserve"
    assert entry["device_family"] == "mobile"
    assert entry["page_title"] == "Reset {DEVICENAME}?"
    assert entry["failure_message"] == "Failed. Tell IT."
    assert len(entry["token"]) >= 32
    assert entry["script"].endswith("reset-ipad-reset.sh")
    assert entry["jamf_id"] is None
    assert entry["profile_status"] == "skipped"  # no checkbox sent
    for key in ("webhook_username", "webhook_password", "api_key"):
        assert entry[key] == "null"


def test_create_rejects_bad_service_name(logged_in_client, jawa_env):
    resp = logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(service_name="reset ipad"),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert "letters, numbers" in resp.get_data(as_text=True)
    assert data_store.get_webhook_by_name("reset ipad") is None


def test_create_rejects_unknown_device_family(logged_in_client, jawa_env):
    resp = logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(device_family="toaster"),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert data_store.get_webhook_by_name("reset-ipad") is None


def test_create_requires_every_page_string(logged_in_client, jawa_env):
    resp = logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(confirm_label=""),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert data_store.get_webhook_by_name("reset-ipad") is None


def test_detail_shows_the_web_clip_url(logged_in_client, jawa_env):
    logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(),
        content_type="multipart/form-data",
    )
    entry = data_store.get_webhook_by_name("reset-ipad")
    body = logged_in_client.get(
        "/automations/selfserve/reset-ipad"
    ).get_data(as_text=True)
    assert "/selfserve/reset-ipad?token=" + entry["token"] in body
    assert "Device family" in body


def test_edit_updates_strings_and_keeps_the_token(
    logged_in_client, jawa_env
):
    logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(),
        content_type="multipart/form-data",
    )
    before = data_store.get_webhook_by_name("reset-ipad")
    form = _create_form(page_title="Wipe {DEVICENAME}")
    del form["new_file"]
    resp = logged_in_client.post(
        "/automations/selfserve/reset-ipad/edit",
        data=dict(form, button_choice="Save"),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    after = data_store.get_webhook_by_name("reset-ipad")
    assert after["page_title"] == "Wipe {DEVICENAME}"
    assert after["token"] == before["token"]
    assert after["script"] == before["script"]


def test_delete_retires_script_and_removes_record(
    logged_in_client, jawa_env
):
    logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(),
        content_type="multipart/form-data",
    )
    entry = data_store.get_webhook_by_name("reset-ipad")
    resp = logged_in_client.post(
        "/automations/selfserve/reset-ipad/delete", data={}
    )
    assert resp.status_code in (200, 302)
    assert data_store.get_webhook_by_name("reset-ipad") is None
    assert (jawa_env.scripts_dir / (
        entry["script"].rsplit("/", 1)[1] + ".old"
    )).exists()


@pytest.mark.parametrize(
    "path",
    ["/automations/selfserve", "/automations/selfserve/new"],
)
def test_console_pages_render(logged_in_client, jawa_env, path):
    assert logged_in_client.get(path).status_code == 200
