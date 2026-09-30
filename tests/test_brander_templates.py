"""Brander end to end, with Jamf Pro faked at the requests layer.

The script is exec'd from its template source with the tokens filled,
then main() runs against a fake Jamf that answers only the three calls
Brander makes (token, device record, Wallpaper command) and fails the
test on any other URL. Covers the legacy layout and wallrender
templates, the value allowlist, the render timeout and the byte budget.
"""

import base64
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import time

import pytest
import requests
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(
    REPO_ROOT, "data", "workflows", "scripts", "self_serve_brander.py"
)
LIB_DIR = os.path.join(REPO_ROOT, "data", "workflows", "lib")
ASSETS = os.path.join(REPO_ROOT, "data", "workflows", "assets", "brander")
SERVER = "https://jamf.test"
WALLPAPER_URL = f"{SERVER}/JSSResource/mobiledevicecommands/command/Wallpaper"


def load_brander(script_path=SCRIPT, template_path="legacy", max_kb=1500):
    with open(SCRIPT, encoding="utf-8") as handle:
        source = handle.read()
    tokens = {
        '"__JAWA_SERVER_URL__"': repr(SERVER),
        '"__JAWA_CLIENT_ID__"': repr("id"),
        '"__JAWA_CLIENT_SECRET__"': repr("secret"),
        "__JAWA_EA_ID__": "0",
        "__JAWA_WALLPAPER_SETTING__": "3",
        '"__JAWA_ASSETS_DIR__"': repr(ASSETS),
        '"__JAWA_FONT_PATH__"': repr("none"),
        '"__JAWA_TEMPLATE_PATH__"': repr(template_path),
        "__JAWA_MAX_KB__": str(max_kb),
    }
    for token, value in tokens.items():
        assert token in source, token
        source = source.replace(token, value)
    spec = importlib.util.spec_from_loader("brander_under_test", loader=None)
    module = importlib.util.module_from_spec(spec)
    # Deployed scripts live in <JAWA>/scripts and find the renderer
    # relative to their own file, so __file__ decides where it looks.
    module.__file__ = script_path
    exec(compile(source, SCRIPT, "exec"), module.__dict__)
    return module


@pytest.fixture(autouse=True)
def _fresh_wallrender():
    """Each test imports the renderer the way the script does."""
    saved = {k: v for k, v in sys.modules.items() if k.startswith("wallrender")}
    for name in saved:
        del sys.modules[name]
    saved_path = list(sys.path)
    yield
    for name in [k for k in sys.modules if k.startswith("wallrender")]:
        del sys.modules[name]
    sys.modules.update(saved)
    sys.path[:] = saved_path


def device_record(**general):
    record = {
        "general": {
            "id": 42,
            "udid": "REAL-UDID",
            "device_name": "Ward 3 iPad",
            "serial_number": "DMPX1234",
            "asset_tag": "HR-0042",
            "model": "iPad",
            "os_type": "iPadOS",
            "os_version": "18.0",
            "phone_number": "555-0100",
        },
        "location": {
            "building": "North Campus",
            "department": "Nursing",
            "username": "nurse.jackie",
            "real_name": "Jackie Peyton",
            "email_address": "jackie@example.org",
        },
        "extension_attributes": [],
    }
    record["general"].update(general)
    return record


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeJamf:
    def __init__(self):
        self.device = device_record()
        self.wallpaper_status = 201
        self.commands = []

    def request(self, method, url, **kwargs):
        if method == "GET" and url == f"{SERVER}/JSSResource/mobiledevices/id/42":
            return FakeResponse(200, {"mobile_device": self.device})
        if method == "GET" and url.startswith(
            f"{SERVER}/JSSResource/mobiledevices/id/"
        ):
            return FakeResponse(404)
        raise AssertionError(f"unexpected Jamf call: {method} {url}")

    def post(self, url, **kwargs):
        if url == f"{SERVER}/api/oauth/token":
            assert kwargs["data"]["client_secret"] == "secret"
            return FakeResponse(200, {"access_token": "t", "expires_in": 300})
        if url == WALLPAPER_URL:
            assert kwargs["headers"]["Authorization"] == "Bearer t"
            self.commands.append(kwargs["data"])
            return FakeResponse(self.wallpaper_status)
        raise AssertionError(f"unexpected Jamf POST: {url}")

    def sent_image(self):
        assert len(self.commands) == 1
        body = self.commands[0]
        assert "<wallpaper_setting>3</wallpaper_setting>" in body
        assert "<id>42</id>" in body
        b64 = re.search(r"<wallpaper_content>(.*)</wallpaper_content>", body)
        return base64.b64decode(b64.group(1))


@pytest.fixture()
def jamf(monkeypatch):
    fake = FakeJamf()
    monkeypatch.setattr(requests, "request", fake.request)
    monkeypatch.setattr(requests, "post", fake.post)

    def _no_get(*a, **k):
        raise AssertionError("Brander must not call requests.get")

    monkeypatch.setattr(requests, "get", _no_get)
    return fake


def run(module, payload=None, monkeypatch=None):
    if payload is None:
        payload = {"event": {"jssID": 42, "udid": "real-udid"}}
    argv = ["brander", json.dumps(payload)]
    old = sys.argv
    sys.argv = argv
    try:
        module.main()
    except SystemExit as exc:
        return exc.code
    finally:
        sys.argv = old
    return 0


def write_template(tmp_path, template):
    path = tmp_path / "template.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    return str(path)


def basic_template(**extra):
    template = {
        "schema_version": 1,
        "canvas": {"width": 300, "height": 600},
        "background": {"color": "#0B2545"},
        "layers": [
            {
                "type": "text",
                "text": "{{device_name}}",
                "box": {"x": 0.05, "y": 0.5, "w": 0.9, "h": 0.08},
                "size": 0.04,
            },
            {
                "type": "qr",
                "data": "jamf-device:{{jss_id}}",
                "box": {"x": 0.3, "y": 0.7, "w": 0.4, "h": 0.2},
            },
        ],
    }
    template.update(extra)
    return template


# --- legacy layout, unchanged when no template is configured ---------------


@pytest.mark.parametrize("setting", ["legacy", "", "  Legacy "])
def test_legacy_layout_sends_the_role_background(jamf, setting):
    module = load_brander(template_path=setting)
    assert run(module) == 0
    with Image.open(io.BytesIO(jamf.sent_image())) as img:
        with Image.open(os.path.join(ASSETS, "null.png")) as bg:
            assert img.size == bg.size


def test_bad_payload_exits_12(jamf):
    assert run(load_brander(), {"event": {"jssID": "42"}}) == 12
    assert jamf.commands == []


def test_udid_mismatch_exits_20(jamf):
    payload = {"event": {"jssID": 42, "udid": "someone-else"}}
    assert run(load_brander(), payload) == 20
    assert jamf.commands == []


def test_unknown_device_exits_21(jamf):
    payload = {"event": {"jssID": 7, "udid": "real-udid"}}
    assert run(load_brander(), payload) == 21


def test_refused_wallpaper_command_exits_24(jamf):
    jamf.wallpaper_status = 400
    assert run(load_brander()) == 24


def test_brander_ea_off_exits_42(jamf):
    jamf.device["extension_attributes"] = [{"name": "Brander", "value": "Off"}]
    assert run(load_brander()) == 42
    assert jamf.commands == []


# --- wallrender templates --------------------------------------------------


def test_template_renders_through_wallrender(jamf, tmp_path):
    module = load_brander(
        template_path=write_template(tmp_path, basic_template())
    )
    assert run(module) == 0
    with Image.open(io.BytesIO(jamf.sent_image())) as img:
        assert img.size == (300, 600)
        assert img.mode == "RGB"
        assert img.getpixel((5, 5)) == (0x0B, 0x25, 0x45)


def test_shipped_example_template_renders(jamf):
    module = load_brander(template_path="example-template.json")
    assert run(module) == 0
    with Image.open(io.BytesIO(jamf.sent_image())) as img:
        assert img.size == (1290, 2796)


def test_relative_template_path_resolves_in_the_assets_dir(jamf, tmp_path):
    assets = tmp_path / "assets"
    shutil.copytree(ASSETS, assets)
    (assets / "ward.json").write_text(
        json.dumps(basic_template()), encoding="utf-8"
    )
    module = load_brander(template_path="ward.json")
    module.ASSETS_DIR = str(assets)
    assert run(module) == 0


def test_template_image_assets_come_from_the_assets_dir(jamf, tmp_path):
    template = basic_template()
    template["layers"].append(
        {
            "type": "image",
            "asset": "brandlogo",
            "box": {"x": 0.3, "y": 0.1, "w": 0.4, "h": 0.2},
        }
    )
    module = load_brander(template_path=write_template(tmp_path, template))
    assert run(module) == 0


def test_values_are_an_allowlist_of_the_verified_record(jamf):
    module = load_brander()
    values = module.build_values(device_record())
    assert values == {
        "device_name": "Ward 3 iPad",
        "serial_number": "DMPX1234",
        "asset_tag": "HR-0042",
        "jss_id": 42,
        "location": {"building": "North Campus"},
    }


def test_person_fields_never_reach_the_wallpaper(jamf, tmp_path):
    """A template naming a field outside the allowlist renders it empty,
    the same pixels as a template without that layer."""
    leaky = basic_template()
    leaky["layers"][0]["text"] = (
        "{{location.username}}{{location.email_address}}"
        "{{general.phone_number}}{{device_name}}"
    )
    run(load_brander(template_path=write_template(tmp_path, leaky)))
    leaked = jamf.sent_image()
    jamf.commands.clear()
    run(load_brander(template_path=write_template(tmp_path, basic_template())))
    assert jamf.sent_image() == leaked


def test_event_params_cannot_supply_values(jamf, tmp_path):
    module = load_brander(
        template_path=write_template(tmp_path, basic_template())
    )
    payload = {
        "event": {"jssID": 42, "udid": "real-udid", "device_name": "PWNED"}
    }
    run(module, payload)
    first = jamf.sent_image()
    jamf.commands.clear()
    run(module)
    assert jamf.sent_image() == first


@pytest.mark.parametrize(
    "content",
    [
        None,  # missing file
        "{not json",
        json.dumps({"schema_version": 99}),
        json.dumps([1, 2, 3]),
        " " * (300 * 1024),  # over the size cap
    ],
)
def test_unusable_template_exits_30(jamf, tmp_path, content):
    path = tmp_path / "template.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert run(load_brander(template_path=str(path))) == 30
    assert jamf.commands == []


def test_template_that_fails_to_render_exits_32(jamf, tmp_path):
    template = basic_template(background={"asset": "no-such-asset"})
    template.pop("canvas")
    module = load_brander(template_path=write_template(tmp_path, template))
    assert run(module) == 32
    assert jamf.commands == []


def test_asset_ids_cannot_leave_the_assets_dir(tmp_path):
    module = load_brander()
    outside = tmp_path / "secret.png"
    Image.new("RGB", (4, 4)).save(outside)
    module.ASSETS_DIR = str(tmp_path / "assets")
    os.mkdir(module.ASSETS_DIR)
    os.symlink(outside, os.path.join(module.ASSETS_DIR, "escape.png"))
    with pytest.raises(KeyError):
        module.read_asset("escape")
    with pytest.raises(KeyError):
        module.read_asset("../secret")


# --- renderer availability and version pin ---------------------------------


def test_missing_renderer_exits_31(jamf, tmp_path):
    script = tmp_path / "scripts" / "brander.py"
    module = load_brander(
        script_path=str(script),
        template_path=write_template(tmp_path, basic_template()),
    )
    assert run(module) == 31
    assert jamf.commands == []


def test_installed_layout_finds_the_renderer(jamf, tmp_path):
    """<JAWA>/scripts/<name>.py finds <JAWA>/data/workflows/lib."""
    root = tmp_path / "jawa"
    shutil.copytree(LIB_DIR, root / "data" / "workflows" / "lib")
    module = load_brander(
        script_path=str(root / "scripts" / "brand-device.py"),
        template_path=write_template(tmp_path, basic_template()),
    )
    assert run(module) == 0


def test_edited_renderer_copy_exits_31(jamf, tmp_path):
    root = tmp_path / "jawa"
    lib = root / "data" / "workflows" / "lib"
    shutil.copytree(LIB_DIR, lib)
    with open(lib / "wallrender" / "schema.py", "a", encoding="utf-8") as fh:
        fh.write("\n# local tweak\n")
    module = load_brander(
        script_path=str(root / "scripts" / "brand-device.py"),
        template_path=write_template(tmp_path, basic_template()),
    )
    assert run(module) == 31


@pytest.mark.parametrize("version", ["9.5.0", "12.0.0", "13.1.0"])
def test_unsupported_pillow_exits_31(jamf, tmp_path, monkeypatch, version):
    import PIL

    monkeypatch.setattr(PIL, "__version__", version)
    module = load_brander(
        template_path=write_template(tmp_path, basic_template())
    )
    assert run(module) == 31


def test_legacy_layout_does_not_need_the_renderer(jamf, tmp_path, monkeypatch):
    import PIL

    monkeypatch.setattr(PIL, "__version__", "13.0.0")
    module = load_brander(script_path=str(tmp_path / "nowhere" / "b.py"))
    assert run(module) == 0


# --- render timeout ----------------------------------------------------------


def test_render_timeout_interrupts_a_slow_render():
    module = load_brander()

    def spin():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pass

    started = time.monotonic()
    with pytest.raises(module.RenderTimeout):
        module.run_with_timeout(spin, 0.2)
    assert time.monotonic() - started < 2


def test_render_timeout_is_not_swallowed_by_broad_handlers():
    module = load_brander()

    def swallow():
        try:
            time.sleep(5)
        except Exception:  # noqa: BLE001 -- what the renderer must not do
            return "swallowed"

    with pytest.raises(module.RenderTimeout):
        module.run_with_timeout(swallow, 0.2)


def test_slow_template_exits_33(jamf, tmp_path, monkeypatch):
    module = load_brander(
        template_path=write_template(tmp_path, basic_template())
    )
    module.RENDER_TIMEOUT = 0.2
    monkeypatch.setattr(module, "render_template", lambda *a: time.sleep(5))
    assert run(module) == 33
    assert jamf.commands == []


def test_slow_encode_counts_against_the_timeout(jamf, tmp_path, monkeypatch):
    module = load_brander(
        template_path=write_template(tmp_path, basic_template())
    )
    module.RENDER_TIMEOUT = 0.2
    monkeypatch.setattr(module, "encode_within_budget", lambda *a: time.sleep(5))
    assert run(module) == 33
    assert jamf.commands == []


def test_asset_ids_with_a_trailing_newline_are_refused(tmp_path):
    module = load_brander()
    module.ASSETS_DIR = str(tmp_path)
    Image.new("RGB", (4, 4)).save(tmp_path / "logo.png")
    assert module.read_asset("logo")
    with pytest.raises(KeyError):
        module.read_asset("logo\n")


# --- byte budget -------------------------------------------------------------


def noisy(size):
    return Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))


def test_small_image_is_sent_as_png():
    module = load_brander()
    data = module.encode_within_budget(Image.new("RGB", (100, 100)), 10_000)
    assert data.startswith(b"\x89PNG")


def test_large_image_falls_back_to_jpeg_within_budget():
    module = load_brander()
    img = noisy((400, 400))
    budget = 200 * 1024
    data = module.encode_within_budget(img, budget)
    assert data.startswith(b"\xff\xd8")
    assert len(data) <= budget
    with Image.open(io.BytesIO(data)) as out:
        assert out.size == (400, 400)


def test_rgba_is_flattened_before_encoding():
    module = load_brander()
    data = module.encode_within_budget(
        Image.new("RGBA", (10, 10), (255, 0, 0, 0)), 10_000
    )
    with Image.open(io.BytesIO(data)) as out:
        assert out.mode == "RGB"


def test_impossible_budget_exits_34(jamf, tmp_path):
    template = basic_template(background={"asset": "noise"})
    template.pop("canvas")
    assets = tmp_path / "assets"
    assets.mkdir()
    noisy((600, 600)).save(assets / "noise.png")
    module = load_brander(
        template_path=write_template(tmp_path, template), max_kb=1
    )
    module.ASSETS_DIR = str(assets)
    assert run(module) == 34
    assert jamf.commands == []
