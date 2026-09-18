"""Device-facing /selfserve/<service-name> receiver.

GET renders the confirmation page from the query string only; POST is
the only thing that spawns a script. Popen is stubbed, so the whole
request -> token -> payload -> execute pipeline runs without a script.
"""

import io
import json
import subprocess

import pytest

from webhook import selfserve_receiver as ssr


class RecordingPopen:
    """Stands in for subprocess.Popen; exit code is class-configurable."""

    calls = []
    return_code = 0

    def __init__(self, args, stdout=None, stderr=None):
        RecordingPopen.calls.append(args)
        self.stdout = io.BytesIO(b"script ran\n")

    def wait(self):
        return RecordingPopen.return_code


@pytest.fixture()
def fake_popen(monkeypatch):
    RecordingPopen.calls = []
    RecordingPopen.return_code = 0
    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    return RecordingPopen


TOKEN = "t0k3n-abc"


def _make_service(jawa_env, **overrides):
    entry = {
        "name": "reset-ipad",
        "tag": "selfserve",
        "url": "https://jamf.example.test",
        "jawa_admin": "pytest-admin",
        "script": str(jawa_env.scripts_dir / "reset.py"),
        "description": "harness self-serve",
        "token": TOKEN,
        "device_family": "mobile",
        "page_title": "Reset {DEVICENAME}?",
        "page_description": "This device will erase itself.",
        "confirm_label": "Erase and reset",
        "success_message": "Done. Put {DEVICENAME} down.",
        "failure_message": "Could not reset. Tell IT.",
        "jamf_id": None,
        "profile_status": "skipped",
        "webhook_username": "null",
        "webhook_password": "null",
        "api_key": "null",
    }
    entry.update(overrides)
    jawa_env.add_webhook(entry)
    return entry


GOOD_QUERY = {
    "token": TOKEN,
    "id": "42",
    "udid": "00008030-000A1B2C3D4E5F67",
    "DEVICENAME": "iPad-042",
}


# --- pure helpers ---


def test_fill_placeholders_substitutes_known_and_blanks_unknown():
    out = ssr.fill_placeholders(
        "Reset {DEVICENAME} ({SERIAL})?", {"DEVICENAME": "iPad-1"}
    )
    assert out == "Reset iPad-1 ()?"


def test_parse_device_request_lifts_reserved_and_keeps_the_rest():
    jss_id, udid, params = ssr.parse_device_request(GOOD_QUERY)
    assert jss_id == 42
    assert udid == "00008030-000A1B2C3D4E5F67"
    assert params == {"DEVICENAME": "iPad-042"}


@pytest.mark.parametrize(
    "missing", ["id", "udid"], ids=["no-id", "no-udid"]
)
def test_parse_device_request_requires_id_and_udid(missing):
    query = dict(GOOD_QUERY)
    del query[missing]
    with pytest.raises(ssr.SelfServeRequestError) as excinfo:
        ssr.parse_device_request(query)
    assert excinfo.value.status == 400


def test_parse_device_request_rejects_non_integer_id():
    with pytest.raises(ssr.SelfServeRequestError) as excinfo:
        ssr.parse_device_request(dict(GOOD_QUERY, id="42; drop"))
    assert excinfo.value.status == 400


def test_token_matches_is_exact_and_never_accepts_empty():
    service = {"token": TOKEN}
    assert ssr.token_matches(service, TOKEN)
    assert not ssr.token_matches(service, "")
    assert not ssr.token_matches(service, None)
    assert not ssr.token_matches(service, TOKEN + "x")
    assert not ssr.token_matches({"token": ""}, "")


def test_build_payload_has_the_documented_shape():
    service = {"name": "reset-ipad", "device_family": "mobile"}
    payload = ssr.build_selfserve_payload(
        service, 42, "UDID-1", {"DEVICENAME": "iPad-042"}, "10.0.0.9"
    )
    assert payload["webhook"]["webhookEvent"] == "SelfServe"
    assert payload["webhook"]["name"] == "reset-ipad"
    assert isinstance(payload["webhook"]["eventTimestamp"], int)
    assert payload["event"] == {
        "jssID": 42,
        "udid": "UDID-1",
        "deviceType": "mobile",
        "params": {"DEVICENAME": "iPad-042"},
        "remoteAddress": "10.0.0.9",
    }
    assert "token" not in json.dumps(payload)


# --- GET: confirmation page ---


def test_get_renders_confirmation_with_filled_placeholders(
    client, jawa_env, fake_popen
):
    _make_service(jawa_env)
    resp = client.get("/selfserve/reset-ipad", query_string=GOOD_QUERY)
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Reset iPad-042?" in body
    assert "Erase and reset" in body
    # Hidden fields carry every parameter back on the POST.
    assert 'name="token"' in body and TOKEN in body
    assert 'name="id"' in body and 'value="42"' in body
    assert 'name="udid"' in body
    assert 'name="DEVICENAME"' in body
    # GET never runs anything.
    assert fake_popen.calls == []


def test_get_escapes_query_values(client, jawa_env, fake_popen):
    _make_service(jawa_env)
    resp = client.get(
        "/selfserve/reset-ipad",
        query_string=dict(GOOD_QUERY, DEVICENAME="<img src=x>"),
    )
    body = resp.get_data(as_text=True)
    assert "<img src=x>" not in body
    assert "&lt;img src=x&gt;" in body


def test_get_with_wrong_token_is_401_and_does_not_echo_the_name(
    client, jawa_env, fake_popen
):
    _make_service(jawa_env)
    resp = client.get(
        "/selfserve/reset-ipad", query_string=dict(GOOD_QUERY, token="nope")
    )
    assert resp.status_code == 401
    assert "reset-ipad" not in resp.get_data(as_text=True)


def test_get_unknown_service_is_401(client, jawa_env, fake_popen):
    resp = client.get("/selfserve/ghost", query_string=GOOD_QUERY)
    assert resp.status_code == 401


def test_get_without_udid_is_400(client, jawa_env, fake_popen):
    _make_service(jawa_env)
    query = dict(GOOD_QUERY)
    del query["udid"]
    resp = client.get("/selfserve/reset-ipad", query_string=query)
    assert resp.status_code == 400


def test_get_with_non_ascii_token_is_401_not_500(
    client, jawa_env, fake_popen
):
    """hmac.compare_digest raises TypeError on non-ASCII str input;
    token_matches must compare as bytes so a bad token is a 401, not
    an unhandled 500."""
    _make_service(jawa_env)
    resp = client.get(
        "/selfserve/reset-ipad", query_string=dict(GOOD_QUERY, token="☃")
    )
    assert resp.status_code == 401
    assert fake_popen.calls == []


def test_get_with_non_ascii_unicode_digit_id_is_400_not_500(
    client, jawa_env, fake_popen
):
    """str.isdigit() accepts Unicode digits (e.g. '²') that
    int() rejects; parse_device_request must reject them as 400
    rather than raising ValueError."""
    _make_service(jawa_env)
    resp = client.get(
        "/selfserve/reset-ipad", query_string=dict(GOOD_QUERY, id="²")
    )
    assert resp.status_code == 400
    assert fake_popen.calls == []


def test_non_selfserve_entry_is_not_a_service(client, jawa_env, fake_popen):
    _make_service(jawa_env, tag="custom")
    resp = client.get("/selfserve/reset-ipad", query_string=GOOD_QUERY)
    assert resp.status_code == 401


def test_selfserve_entry_cannot_be_fired_through_hooks(
    client, jawa_env, fake_popen
):
    """The three "null" auth keys would otherwise make /hooks/<name>
    an open, token-free way to run a self-serve script."""
    _make_service(jawa_env)
    resp = client.post("/hooks/reset-ipad", json={"webhook": {}})
    assert resp.status_code == 401
    assert fake_popen.calls == []
