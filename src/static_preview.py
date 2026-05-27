from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from article_list_mvp import run_mvp
from server import build_index_html

BASE_DIR = Path(__file__).resolve().parents[1]
ADOBE = BASE_DIR / "fixtures" / "adobe_sample.json"
RSS = BASE_DIR / "fixtures" / "rss_sample.xml"
HOME = BASE_DIR / "fixtures" / "home_positions_sample.json"
OUT = Path("/tmp/tc_artikelliste_preview.html")


def build_preview_html(data: list[dict[str, object]]) -> str:
    return build_index_html(
        ui_path="/",
        api_path="/api/articles",
        health_path="/healthz",
        csv_path="/api/export/csv",
    )


def main() -> int:
    data = run_mvp(ADOBE, RSS, HOME)
    html = build_preview_html(data)
    OUT.write_text(html, encoding="utf-8")
    print(f"Vorschau erstellt: {OUT}")
    print(f"Im Browser öffnen: file://{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
