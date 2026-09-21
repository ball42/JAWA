"""Console side of self-serve automations: the type handler."""

import io
import plistlib
import xml.etree.ElementTree as ET

import pytest
import requests

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


class _ProfileJamf:
    """Captures profile calls and answers like Jamf Pro Classic API."""

    def __init__(self, fake_response_cls, fake_post, status=201):
        self._fake_response_cls = fake_response_cls
        self._fake_post = fake_post
        self.posts = []
        self.puts = []
        self.status = status
        self.put_status = 201

    def post(self, url, **kwargs):
        if "/JSSResource/mobiledeviceconfigurationprofiles/id/0" in url:
            self.posts.append((url, kwargs))
            return self._fake_response_cls(
                {},
                status_code=self.status,
                text="<mobile_device_configuration_profile><id>55</id>"
                "</mobile_device_configuration_profile>",
            )
        return self._fake_post(url, **kwargs)

    def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        return self._fake_response_cls(
            {}, status_code=self.put_status, text="<id>55</id>"
        )


@pytest.fixture()
def profile_jamf(monkeypatch, fake_jamf, jamf_fake_http):
    fake_response_cls, fake_post = jamf_fake_http
    jamf = _ProfileJamf(fake_response_cls, fake_post)
    monkeypatch.setattr(requests, "post", jamf.post)
    monkeypatch.setattr(requests, "put", jamf.put)
    return jamf


def test_plist_is_a_managed_web_clip_with_the_service_url():
    entry = {"name": "reset-ipad", "page_title": "Reset {DEVICENAME}?"}
    url = "https://jawa.example.test/selfserve/reset-ipad?token=T&id=$JSSID"
    plist = plistlib.loads(ssh.build_webclip_plist(entry, url).encode())
    assert plist["PayloadType"] == "Configuration"
    clip = plist["PayloadContent"][0]
    assert clip["PayloadType"] == "com.apple.webClip.managed"
    assert clip["URL"] == url
    assert clip["Label"] == "Reset?"  # placeholders stripped
    assert clip["IsRemovable"] is False
    assert clip["FullScreen"] is True
    assert (
        clip["PayloadIdentifier"]
        == "com.jamf.jawa.selfserve.reset-ipad.webclip"
    )


def test_profile_xml_uses_only_mobile_schema_elements():
    """Jamf Pro's mobiledeviceconfigurationprofiles schema has
    deployment_method; distribution_method belongs to the macOS
    (osxconfigurationprofiles) schema. Sending it makes Jamf answer
    400 "Error in XML file. Possible mismatch between resource
    specified in the URL and XML file" (seen live 2026-09-21)."""
    entry = {"name": "reset-ipad", "page_title": "Reset"}
    root = ET.fromstring(ssh.build_profile_xml(entry, "https://x/"))
    assert root.find("general/distribution_method") is None
    assert root.findtext("general/deployment_method") == (
        "Install Automatically"
    )


def test_profile_xml_is_well_formed_and_unscoped():
    entry = {"name": "reset-ipad", "page_title": "Reset"}
    root = ET.fromstring(ssh.build_profile_xml(entry, "https://x/?a=1&b=2"))
    assert root.tag == "configuration_profile"
    assert (
        root.findtext("general/name")
        == "JAWA Self-Serve: Reset (reset-ipad)"
    )
    assert root.findtext("scope/all_mobile_devices") == "false"
    assert root.findtext("general/redeploy_on_update") == "All"
    payloads = root.findtext("general/payloads")
    assert "com.apple.webClip.managed" in payloads
    assert "a=1&amp;b=2" in payloads  # escaped inside the plist string


def test_profile_xml_names_differ_for_same_title_different_service():
    """Jamf Pro profile names must be unique; two services created from
    the same template share page_title by default, so the service
    name must disambiguate them."""
    entry_a = {"name": "reset-ipad-a", "page_title": "Reset"}
    entry_b = {"name": "reset-ipad-b", "page_title": "Reset"}
    root_a = ET.fromstring(ssh.build_profile_xml(entry_a, "https://x/"))
    root_b = ET.fromstring(ssh.build_profile_xml(entry_b, "https://x/"))
    assert root_a.findtext("general/name") != root_b.findtext(
        "general/name"
    )


def test_create_with_checkbox_creates_profile_and_stores_id(
    logged_in_client, jawa_env, profile_jamf
):
    logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(create_profile="on"),
        content_type="multipart/form-data",
    )
    entry = data_store.get_webhook_by_name("reset-ipad")
    assert entry["jamf_id"] == "55"
    assert entry["profile_status"] == "created"
    assert len(profile_jamf.posts) == 1
    url, kwargs = profile_jamf.posts[0]
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert entry["token"] in kwargs["data"]


def test_create_refreshes_the_token_before_posting_the_profile(
    logged_in_client, jawa_env, profile_jamf, monkeypatch
):
    """_fresh_token() calls get_token(), which stores the new token in
    flask.session and returns None; the stale token snapshot in
    session_data must be replaced with the fresh one before the
    profile request is built, or Jamf Pro gets the old token."""
    from flask import session

    monkeypatch.setattr(ssh, "validate_token", lambda *_a, **_k: False)

    def _refresh(*_a, **_k):
        session["token"] = "fresh-token"
        return None

    monkeypatch.setattr(ssh, "get_token", _refresh)
    logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(create_profile="on"),
        content_type="multipart/form-data",
    )
    assert len(profile_jamf.posts) == 1
    _url, kwargs = profile_jamf.posts[0]
    assert kwargs["headers"]["Authorization"] == "Bearer fresh-token"


def test_create_without_checkbox_skips_jamf(
    logged_in_client, jawa_env, profile_jamf
):
    logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(),
        content_type="multipart/form-data",
    )
    entry = data_store.get_webhook_by_name("reset-ipad")
    assert entry["profile_status"] == "skipped"
    assert profile_jamf.posts == []


def test_profile_failure_still_creates_the_service(
    logged_in_client, jawa_env, profile_jamf
):
    profile_jamf.status = 409
    logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(create_profile="on"),
        content_type="multipart/form-data",
    )
    entry = data_store.get_webhook_by_name("reset-ipad")
    assert entry is not None
    assert entry["jamf_id"] is None
    assert entry["profile_status"] == "failed"


def test_edit_updates_the_profile_when_one_exists(
    logged_in_client, jawa_env, profile_jamf
):
    logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(create_profile="on"),
        content_type="multipart/form-data",
    )
    form = _create_form(page_title="Wipe it")
    del form["new_file"]
    logged_in_client.post(
        "/automations/selfserve/reset-ipad/edit",
        data=dict(form, button_choice="Save"),
        content_type="multipart/form-data",
    )
    assert any(
        "/mobiledeviceconfigurationprofiles/id/55" in url
        for url, _ in profile_jamf.puts
    )


def test_delete_retires_the_profile(
    logged_in_client, jawa_env, profile_jamf
):
    logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(create_profile="on"),
        content_type="multipart/form-data",
    )
    logged_in_client.post(
        "/automations/selfserve/reset-ipad/delete", data={}
    )
    url, kwargs = profile_jamf.puts[-1]
    assert "/mobiledeviceconfigurationprofiles/id/55" in url
    assert ".old." in kwargs["data"]


def test_computer_family_never_gets_a_mobile_profile(
    logged_in_client, jawa_env, profile_jamf
):
    """Web Clips are a mobile device profile payload; Jamf Pro has no
    computer equivalent, so a computer-family service must be skipped
    even with the checkbox on."""
    logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(
            device_family="computer", create_profile="on"
        ),
        content_type="multipart/form-data",
    )
    entry = data_store.get_webhook_by_name("reset-ipad")
    assert entry["jamf_id"] is None
    assert entry["profile_status"] == "skipped"
    assert profile_jamf.posts == []


def test_token_check_failure_still_creates_the_service(
    logged_in_client, jawa_env, profile_jamf, monkeypatch
):
    """A malformed/expired-token check must degrade to
    profile_status "failed", never bubble out of the "never raises"
    profile helpers (and never 500 the create request)."""

    def _boom(*_a, **_k):
        raise ValueError("bad expires value")

    monkeypatch.setattr(ssh, "validate_token", _boom)
    resp = logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(create_profile="on"),
        content_type="multipart/form-data",
    )
    assert resp.status_code != 500
    entry = data_store.get_webhook_by_name("reset-ipad")
    assert entry is not None
    assert entry["jamf_id"] is None
    assert entry["profile_status"] == "failed"
    assert profile_jamf.posts == []


def test_edit_marks_the_profile_failed_when_jamf_refuses_the_update(
    logged_in_client, jawa_env, profile_jamf
):
    logged_in_client.post(
        "/automations/selfserve/new",
        data=_create_form(create_profile="on"),
        content_type="multipart/form-data",
    )
    profile_jamf.put_status = 409
    form = _create_form(page_title="Wipe it")
    del form["new_file"]
    logged_in_client.post(
        "/automations/selfserve/reset-ipad/edit",
        data=dict(form, button_choice="Save"),
        content_type="multipart/form-data",
    )
    entry = data_store.get_webhook_by_name("reset-ipad")
    assert entry["profile_status"] == "failed"
