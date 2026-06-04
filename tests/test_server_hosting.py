import server
import pytest


def test_load_articles_uses_cache(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_run_mvp(**kwargs):  # type: ignore[no-untyped-def]
        _ = kwargs
        calls["count"] += 1
        return [{"call": calls["count"]}]

    monkeypatch.setattr(server, "run_mvp", fake_run_mvp)
    monkeypatch.setattr(server, "CACHE_SECONDS", 60)
    monkeypatch.setattr(server, "_CACHE_DATA", None)
    monkeypatch.setattr(server, "_CACHE_EXPIRES_AT", 0.0)
    monkeypatch.setattr(server, "FIXTURE_MODE_EXPLICIT", True)  # allow mock data in test

    first = server.load_articles(force_refresh=False)
    second = server.load_articles(force_refresh=False)

    assert first == second
    assert calls["count"] == 1


def test_load_articles_force_refresh_bypasses_cache(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_run_mvp(**kwargs):  # type: ignore[no-untyped-def]
        _ = kwargs
        calls["count"] += 1
        return [{"call": calls["count"]}]

    monkeypatch.setattr(server, "run_mvp", fake_run_mvp)
    monkeypatch.setattr(server, "CACHE_SECONDS", 60)
    monkeypatch.setattr(server, "_CACHE_DATA", None)
    monkeypatch.setattr(server, "_CACHE_EXPIRES_AT", 0.0)
    monkeypatch.setattr(server, "FIXTURE_MODE_EXPLICIT", True)  # allow mock data in test

    first = server.load_articles(force_refresh=False)
    second = server.load_articles(force_refresh=True)

    assert first != second
    assert calls["count"] == 2


def test_env_helpers_parse_expected_values(monkeypatch) -> None:
    monkeypatch.setenv("TEST_BOOL", "YeS")
    monkeypatch.setenv("TEST_INT", "-9")
    monkeypatch.setenv("TEST_INT_INVALID", "abc")

    assert server._env_bool("TEST_BOOL", default=False) is True
    assert server._env_bool("MISSING_BOOL", default=True) is True
    assert server._env_int("TEST_INT", default=5, minimum=0) == 0
    assert server._env_int("TEST_INT_INVALID", default=7, minimum=0) == 7


def test_resolve_listen_port_prefers_tc_port(monkeypatch) -> None:
    monkeypatch.setenv("TC_PORT", "8105")
    monkeypatch.setenv("PORT", "8110")

    assert server._resolve_listen_port() == (8105, "TC_PORT")


def test_resolve_listen_port_uses_platform_port(monkeypatch) -> None:
    monkeypatch.delenv("TC_PORT", raising=False)
    monkeypatch.setenv("PORT", "8110")

    assert server._resolve_listen_port() == (8110, "PORT")


def test_resolve_listen_port_uses_default_8099(monkeypatch) -> None:
    monkeypatch.delenv("TC_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)

    assert server._resolve_listen_port() == (8099, "default")


def test_normalize_base_path() -> None:
    assert server._normalize_base_path("/") == "/"
    assert server._normalize_base_path(" /tc-artikelliste/ ") == "/tc-artikelliste"
    assert server._normalize_base_path("tc-artikelliste") == "/tc-artikelliste"
    assert server._normalize_base_path("///tc-artikelliste///api///") == "/tc-artikelliste/api"


def test_path_from_request_handles_prefixed_routes(monkeypatch) -> None:
    monkeypatch.setattr(server, "BASE_PATH", "/tc-artikelliste")

    assert server._path_from_request("/tc-artikelliste") == "/"
    assert server._path_from_request("/tc-artikelliste/") == "/"
    assert server._path_from_request("/tc-artikelliste//api/articles") == "/api/articles"
    assert server._path_from_request("/tc-artikelliste/api/articles") == "/api/articles"
    assert server._path_from_request("/api/articles") == "/api/articles"


def test_base_path_for_links_uses_request_shape(monkeypatch) -> None:
    monkeypatch.setattr(server, "BASE_PATH", "/tc-artikelliste")

    assert server._base_path_for_links("/tc-artikelliste") == "/tc-artikelliste"
    assert server._base_path_for_links("/tc-artikelliste/api/articles") == "/tc-artikelliste"
    assert server._base_path_for_links("/api/articles") == "/"


def test_public_base_url_helpers(monkeypatch) -> None:
    assert server._normalize_public_base_url(" https://tc.example.com/tc-artikelliste/ ") == "https://tc.example.com/tc-artikelliste"
    assert server._normalize_public_base_url("   ") is None

    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://tc.example.com/tc-artikelliste")
    assert server._public_url("/") == "https://tc.example.com/tc-artikelliste/"
    assert server._public_url("/api/articles") == "https://tc.example.com/tc-artikelliste/api/articles"


def test_build_index_html_uses_injected_paths() -> None:
    html = server.build_index_html(
        ui_path="/tc-artikelliste/",
        api_path="/tc-artikelliste/api/articles",
        health_path="/tc-artikelliste/healthz",
    )

    assert '/tc-artikelliste/' in html
    assert '/tc-artikelliste/api/articles' in html
    assert '/tc-artikelliste/healthz' in html
    assert 'id="copy-share-link"' in html
    assert "window.location.pathname" in html
    assert "new URL('api/articles', resolvedUiUrl)" in html
    assert "fetch(resolvedApiUrl + tabParam)" in html


def test_parse_optional_port_handles_invalid_values() -> None:
    assert server._parse_optional_port(None) is None
    assert server._parse_optional_port("  ") is None
    assert server._parse_optional_port("abc") is None
    assert server._parse_optional_port("-1") == 1
    assert server._parse_optional_port("8123") == 8123


def test_resolve_listen_port_uses_default_when_env_invalid(monkeypatch) -> None:
    monkeypatch.setenv("TC_PORT", "NaN")
    monkeypatch.setenv("PORT", "")

    port, source = server._resolve_listen_port()

    assert port == 8099
    assert source == "default"


def test_map_editorial_one_row_maps_rank_fields() -> None:
    mapped = server._map_editorial_one_row(
        {
            "url": "https://www.bild.de/a/test-123456?x=1",
            "title": "Test",
            "_rank_home": 4,
            "_rank_readers": -321,
            "published": "2026-04-23T09:00:00Z",
        }
    )

    assert mapped is not None
    assert mapped["canonical_url"] == "https://bild.de/a/test-123456"
    assert mapped["home_position"] == 4
    assert mapped["live_readers"] == 321
    assert mapped["published_at"] == "2026-04-23T09:00:00Z"
    assert "editorial_one" in mapped["source_flags"]


def test_load_articles_prefers_editorial_one_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(server, "EDITORIAL_ONE_ENABLED", True)
    monkeypatch.setattr(server, "EDITORIAL_ONE_STRICT", False)
    monkeypatch.setattr(server, "CACHE_SECONDS", 0)
    monkeypatch.setattr(server, "_CACHE_DATA", None)
    monkeypatch.setattr(server, "_CACHE_EXPIRES_AT", 0.0)

    calls = {"mvp": 0}

    def fake_run_mvp(**kwargs):  # type: ignore[no-untyped-def]
        _ = kwargs
        calls["mvp"] += 1
        return [{"source": "mvp"}]

    monkeypatch.setattr(server, "run_mvp", fake_run_mvp)
    monkeypatch.setattr(server, "_load_editorial_one_articles", lambda force_refresh=False: [{"source": "editorial"}])

    data = server.load_articles(force_refresh=False)

    assert data == [{"source": "editorial"}]
    assert calls["mvp"] == 0


def test_load_articles_falls_back_when_editorial_one_fails(monkeypatch) -> None:
    monkeypatch.setattr(server, "EDITORIAL_ONE_ENABLED", True)
    monkeypatch.setattr(server, "EDITORIAL_ONE_STRICT", False)
    monkeypatch.setattr(server, "CACHE_SECONDS", 0)
    monkeypatch.setattr(server, "_CACHE_DATA", None)
    monkeypatch.setattr(server, "_CACHE_EXPIRES_AT", 0.0)

    monkeypatch.setattr(server, "_load_editorial_one_articles", lambda force_refresh=False: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(server, "run_mvp", lambda **kwargs: [{"source": "mvp"}])  # type: ignore[no-untyped-def]

    data = server.load_articles(force_refresh=False)
    assert data == [{"source": "mvp"}]


def test_load_articles_falls_back_when_editorial_one_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(server, "EDITORIAL_ONE_ENABLED", True)
    monkeypatch.setattr(server, "EDITORIAL_ONE_STRICT", False)
    monkeypatch.setattr(server, "CACHE_SECONDS", 0)
    monkeypatch.setattr(server, "_CACHE_DATA", None)
    monkeypatch.setattr(server, "_CACHE_EXPIRES_AT", 0.0)

    monkeypatch.setattr(server, "_load_editorial_one_articles", lambda force_refresh=False: [])
    monkeypatch.setattr(server, "run_mvp", lambda **kwargs: [{"source": "mvp"}])  # type: ignore[no-untyped-def]

    data = server.load_articles(force_refresh=False)
    assert data == [{"source": "mvp"}]


def test_load_articles_raises_when_editorial_one_strict(monkeypatch) -> None:
    monkeypatch.setattr(server, "EDITORIAL_ONE_ENABLED", True)
    monkeypatch.setattr(server, "EDITORIAL_ONE_STRICT", True)
    monkeypatch.setattr(server, "CACHE_SECONDS", 0)
    monkeypatch.setattr(server, "_CACHE_DATA", None)
    monkeypatch.setattr(server, "_CACHE_EXPIRES_AT", 0.0)
    monkeypatch.setattr(server, "_load_editorial_one_articles", lambda force_refresh=False: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        server.load_articles(force_refresh=False)
