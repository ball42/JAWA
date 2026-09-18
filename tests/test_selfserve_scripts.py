"""Both self-serve script templates refuse to act on a UDID mismatch.

Return to Service and Brander are triggered from the device itself
via a caller-supplied Jamf Pro id; the id alone must never be enough
to touch the device (ADR-0012) -- fetch_verified_device() must refuse
unless the UDID Jamf Pro has on file for that id matches the caller's
own UDID. Lock that in for both templates, with no network: the stub
replaces the only network call fetch_verified_device() makes.
"""

import importlib.util
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "data", "workflows", "scripts")


def _load_module(basename):
    """exec() the script's source into a fresh module namespace, the
    same technique as test_brander_script_generates_an_image_without_
    a_font_file in tests/test_selfserve_templates.py. The bare numeric
    template tokens must become literals for the module to exec at
    all; __JAWA_WALLPAPER_SETTING__ and __JAWA_EA_ID__ are only
    present in Brander, so the replace is a no-op for RTS.
    """
    path = os.path.join(SCRIPTS_DIR, basename)
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    source = source.replace("__JAWA_EA_ID__", "0").replace(
        "__JAWA_WALLPAPER_SETTING__", "3"
    )
    spec = importlib.util.spec_from_loader(basename, loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, path, "exec"), module.__dict__)
    return module


@pytest.fixture()
def rts_module():
    module = _load_module("self_serve_return_to_service.py")
    module.perform_api_call = lambda *a, **k: {
        "udid": "REAL-UDID",
        "managementId": "m",
    }
    return module


@pytest.fixture()
def brander_module():
    module = _load_module("self_serve_brander.py")
    module.perform_api_call = lambda *a, **k: {
        "mobile_device": {"general": {"udid": "REAL-UDID"}}
    }
    return module


def test_rts_refuses_a_udid_that_does_not_match(rts_module):
    with pytest.raises(SystemExit) as excinfo:
        rts_module.fetch_verified_device(42, "OTHER-UDID")
    assert excinfo.value.code == 20


def test_rts_accepts_a_matching_udid_case_insensitively(rts_module):
    device = rts_module.fetch_verified_device(42, "real-udid")
    assert device == {"udid": "REAL-UDID", "managementId": "m"}


def test_brander_refuses_a_udid_that_does_not_match(brander_module):
    with pytest.raises(SystemExit) as excinfo:
        brander_module.fetch_verified_device(42, "OTHER-UDID")
    assert excinfo.value.code == 20


def test_brander_accepts_a_matching_udid_case_insensitively(
    brander_module,
):
    device = brander_module.fetch_verified_device(42, "real-udid")
    assert device == {"general": {"udid": "REAL-UDID"}}
