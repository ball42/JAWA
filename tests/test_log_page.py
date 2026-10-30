"""The log page: filters in the URL, follow mode, and the level buttons."""


def test_level_buttons_state_is_not_colour_only(logged_in_client, jawa_env):
    body = logged_in_client.get("/log/home.html").data.decode()
    assert 'data-level="all" aria-pressed="true"' in body
    assert 'data-level="error" aria-pressed="false"' in body
    # The active button also shows a check mark glyph.
    assert 'class="level-check"' in body
    assert "✓" in body


def test_log_page_reads_filters_from_the_url(logged_in_client, jawa_env):
    body = logged_in_client.get("/log/home.html?q=x&level=info").data.decode()
    for needle in (
        "URLSearchParams(location.search)",
        "history.replaceState",
        "scrollToEnd()",
        "Following: refreshes every 10 s",
        'id="followToggle"',
    ):
        assert needle in body
