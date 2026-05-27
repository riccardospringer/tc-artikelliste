import email.message
from pathlib import Path
from urllib.error import URLError

import article_list_mvp as mvp
from article_list_mvp import ConnectorConfig, build_prioritized_article_list, load_adobe, load_home_positions, load_rss, run_mvp


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_end_to_end_filter_and_ranking() -> None:
    result = run_mvp(
        adobe_file=FIXTURES / "adobe_sample.json",
        rss_file=FIXTURES / "rss_sample.xml",
        home_file=FIXTURES / "home_positions_sample.json",
    )

    urls = [r["canonical_url"] for r in result]

    assert "https://bild.de/news/vermischtes/test-b-654321" not in urls
    assert "https://bild.de/news/panorama/test-d-888888" not in urls

    # Reihenfolge: Home-Position zuerst (1,2,3)
    assert urls[0] == "https://bild.de/sport/fussball/test-c-777777"
    assert urls[1] == "https://bild.de/politik/inland/test-a-123456"
    assert urls[2] == "https://bild.de/wirtschaft/test-e-999999"


def test_matching_merges_adobe_rss_home() -> None:
    adobe = load_adobe(FIXTURES / "adobe_sample.json")
    rss = load_rss(FIXTURES / "rss_sample.xml")
    home = load_home_positions(FIXTURES / "home_positions_sample.json")

    ranked = build_prioritized_article_list(adobe, rss, home)
    article_c = next(r for r in ranked if r.canonical_url.endswith("/test-c-777777"))

    assert "adobe" in article_c.source_flags
    assert "rss" in article_c.source_flags
    assert "home" in article_c.source_flags
    assert article_c.home_position == 1
    assert article_c.live_readers == 900


def test_live_connector_retries_with_backoff(monkeypatch) -> None:
    calls = {"count": 0}
    sleeps: list[float] = []

    class _FakeResponse:
        def __init__(self, payload: str) -> None:
            self._payload = payload.encode("utf-8")
            self.headers = email.message.Message()
            self.headers["Content-Type"] = "application/json; charset=utf-8"

        def read(self) -> bytes:
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        _ = (request, timeout)
        calls["count"] += 1
        if calls["count"] < 3:
            raise URLError("temporary")
        return _FakeResponse('[{"url":"https://www.bild.de/test-123456","workflow_status":"freigegeben"}]')

    monkeypatch.setattr(mvp, "urlopen", fake_urlopen)

    cfg = ConnectorConfig(timeout_seconds=0.1, max_retries=3, backoff_seconds=0.25, backoff_factor=2.0, max_backoff_seconds=10.0)
    payload = mvp._read_with_retry("https://example.com/adobe.json", cfg, sleep_fn=sleeps.append)

    assert "test-123456" in payload
    assert calls["count"] == 3
    assert sleeps == [0.25, 0.5]


def test_unified_model_accepts_mixed_source_fields() -> None:
    adobe = [{"article_url": "https://www.bild.de/a/test-z-444444?x=1", "content_id": "444444", "headline": "Z", "active_users": "77"}]
    rss = [{"url": "https://www.bild.de/a/test-z-444444", "guid": "z-1", "published_at": "2026-04-15T08:00:00Z"}]
    home = [{"link": "https://www.bild.de/a/test-z-444444", "home_position": "5"}]

    ranked = build_prioritized_article_list(adobe, rss, home)

    assert len(ranked) == 1
    assert ranked[0].canonical_url == "https://bild.de/a/test-z-444444"
    assert ranked[0].home_position == 5
    assert ranked[0].live_readers == 77
    assert ranked[0].source_flags == {"adobe", "rss", "home"}


def test_api_check_retries_http_sources(monkeypatch) -> None:
    calls = {"adobe": 0, "rss": 0}
    sleeps: list[float] = []

    class _FakeResponse:
        status = 200
        headers = email.message.Message()

        def read(self, size: int = -1) -> bytes:
            _ = size
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        _ = timeout
        url = request.full_url
        if "adobe" in url:
            calls["adobe"] += 1
            if calls["adobe"] == 1:
                raise URLError("temporary adobe outage")
        elif "rss" in url:
            calls["rss"] += 1
        return _FakeResponse()

    monkeypatch.setattr(mvp, "urlopen", fake_urlopen)
    cfg = ConnectorConfig(timeout_seconds=0.1, max_retries=2, backoff_seconds=0.2, backoff_factor=2.0, max_backoff_seconds=5.0)

    result = mvp.check_live_connector_apis(
        "https://example.internal/adobe/live.json",
        "https://example.internal/rss/live.xml",
        config=cfg,
        sleep_fn=sleeps.append,
    )

    assert result == {"adobe": "ok", "rss": "ok"}
    assert calls["adobe"] == 2
    assert calls["rss"] == 1
    assert sleeps == [0.2]


def test_ignores_rows_without_url_or_cms_id() -> None:
    ranked = build_prioritized_article_list(
        adobe_rows=[
            {"title": "Ohne Key A", "workflow_status": "live"},
            {"title": "Ohne Key B", "status": "live"},
        ],
        rss_items=[],
        home_rows=[],
    )

    assert ranked == []


def test_filters_status_alias_article_status() -> None:
    ranked = build_prioritized_article_list(
        adobe_rows=[
            {
                "url": "https://www.bild.de/politik/test-123456",
                "article_status": "redigiert",
                "live_readers": 999,
            }
        ],
        rss_items=[],
        home_rows=[],
    )

    assert ranked == []


def test_excludes_when_rss_has_excluded_status_alias() -> None:
    adobe: list[dict[str, object]] = []
    rss = [
        {
            "title": "Artikel X",
            "url": "https://www.bild.de/politik/inland/test-x-111111",
            "workflowStatus": " Redigiert ",
        }
    ]
    home = [{"url": "https://www.bild.de/politik/inland/test-x-111111", "position": 1}]

    ranked = build_prioritized_article_list(adobe, rss, home)
    assert ranked == []


def test_excludes_when_adobe_has_mapped_status_spelling_variant() -> None:
    adobe = [
        {
            "url": "https://www.bild.de/news/test-y-222222",
            "workflow-state": "Zum_Verbauen",
            "live_readers": 9999,
        },
        {
            "url": "https://www.bild.de/news/test-z-333333",
            "workflow state": "freigegeben",
            "live_readers": 10,
        },
    ]
    rss: list[dict[str, object]] = []
    home: list[dict[str, object]] = []

    ranked = build_prioritized_article_list(adobe, rss, home)
    urls = [r.canonical_url for r in ranked]

    assert "https://bild.de/news/test-y-222222" not in urls
    assert "https://bild.de/news/test-z-333333" in urls


def test_excludes_when_any_source_has_excluded_status() -> None:
    adobe = [
        {
            "url": "https://www.bild.de/news/test-cross-444444",
            "workflow_status": "freigegeben",
            "live_readers": 1234,
        }
    ]
    rss = [
        {
            "url": "https://www.bild.de/news/test-cross-444444",
            "articleStatus": " redigiert ",
        }
    ]
    home = [{"url": "https://www.bild.de/news/test-cross-444444", "position": 1}]

    ranked = build_prioritized_article_list(adobe, rss, home)
    assert ranked == []


def test_filters_camelcase_status_alias_from_rss() -> None:
    ranked = build_prioritized_article_list(
        adobe_rows=[],
        rss_items=[
            {
                "url": "https://www.bild.de/wirtschaft/test-rss-555555",
                "articleStatus": "zum verbauen",
            }
        ],
        home_rows=[],
    )

    assert ranked == []


def test_filters_status_aliases_from_adobe_csv(tmp_path) -> None:
    adobe_csv = tmp_path / "adobe.csv"
    adobe_csv.write_text(
        "\n".join(
            [
                "link,Artikel Status,active_users",
                "https://www.bild.de/news/test-csv-111111,redigiert,100",
                "https://www.bild.de/news/test-csv-222222,freigegeben,50",
            ]
        ),
        encoding="utf-8",
    )

    adobe = load_adobe(adobe_csv)
    ranked = build_prioritized_article_list(adobe, rss_items=[], home_rows=[])
    urls = [row.canonical_url for row in ranked]

    assert "https://bild.de/news/test-csv-111111" not in urls
    assert "https://bild.de/news/test-csv-222222" in urls


def test_filters_status_aliases_from_rss_xml(tmp_path) -> None:
    rss_xml = tmp_path / "rss.xml"
    rss_xml.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tc="https://example.com/tc">
  <channel>
    <item>
      <title>Artikel A</title>
      <link>https://www.bild.de/politik/test-xml-111111</link>
      <guid>a-111111</guid>
      <pubDate>Wed, 15 Apr 2026 08:00:00 +0000</pubDate>
      <workflowStatus> redigiert </workflowStatus>
    </item>
    <item>
      <title>Artikel B</title>
      <link>https://www.bild.de/politik/test-xml-222222</link>
      <guid>b-222222</guid>
      <pubDate>Wed, 15 Apr 2026 08:01:00 +0000</pubDate>
      <tc:workflow-status>Zum_Verbauen</tc:workflow-status>
    </item>
    <item>
      <title>Artikel C</title>
      <link>https://www.bild.de/politik/test-xml-333333</link>
      <guid>c-333333</guid>
      <pubDate>Wed, 15 Apr 2026 08:02:00 +0000</pubDate>
      <status>freigegeben</status>
    </item>
  </channel>
</rss>
""",
        encoding="utf-8",
    )

    rss = load_rss(rss_xml)
    ranked = build_prioritized_article_list(adobe_rows=[], rss_items=rss, home_rows=[])
    urls = [row.canonical_url for row in ranked]

    assert "https://bild.de/politik/test-xml-111111" not in urls
    assert "https://bild.de/politik/test-xml-222222" not in urls
    assert "https://bild.de/politik/test-xml-333333" in urls
