"""J41 page layout: tool and short pages use the compact hero, the log
page fills the window, and the log toolbar has a phone level menu."""

import pytest


@pytest.mark.parametrize("path", ["/log/home.html", "/search?q=x", "/branding", "/cleanup",
                                  "/setup/credentials", "/resources/files"])
def test_short_and_tool_pages_use_the_compact_hero(logged_in_client, jawa_env, path):
    body = logged_in_client.get(path).get_data(as_text=True)
    assert 'class="hero-section hero-section--compact"' in body


@pytest.mark.parametrize("path", ["/dashboard", "/automations", "/setup", "/templates"])
def test_list_and_form_pages_keep_the_full_hero(logged_in_client, jawa_env, path):
    body = logged_in_client.get(path).get_data(as_text=True)
    assert "hero-section--compact" not in body


def test_the_log_page_fills_the_window(logged_in_client, jawa_env):
    body = logged_in_client.get("/log/home.html").get_data(as_text=True)
    assert 'class="jawa-bg d-flex flex-column page-fill"' in body
    assert 'id="levelSelect"' in body


def test_other_pages_scroll_normally(logged_in_client, jawa_env):
    body = logged_in_client.get("/dashboard").get_data(as_text=True)
    assert "page-fill" not in body


def test_the_sign_in_icon_scales(client, jawa_env):
    body = client.get("/home.html").get_data(as_text=True)
    assert 'class="img-fluid login-icon"' in body
