from __future__ import annotations

import argparse
import json
from pathlib import Path

from article_list_mvp import ConnectorConfig, run_mvp


def main() -> int:
    parser = argparse.ArgumentParser(description="TC-1 MVP: priorisierte bild.de-Textliste erzeugen")
    parser.add_argument("--adobe", required=True, help="Pfad zu Adobe-Daten (JSON oder CSV)")
    parser.add_argument("--rss", required=True, help="Pfad zu RSS-Daten (XML oder JSON)")
    parser.add_argument("--home", required=True, help="Pfad zu Home-Position-Daten (JSON)")
    parser.add_argument("--out", default="", help="Optionaler Ausgabepfad für JSON")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP Timeout in Sekunden")
    parser.add_argument("--retries", type=int, default=3, help="Anzahl HTTP-Retries")
    parser.add_argument("--backoff", type=float, default=0.5, help="Backoff-Basis in Sekunden")
    parser.add_argument("--backoff-factor", type=float, default=2.0, help="Exponentieller Backoff-Faktor")
    parser.add_argument("--backoff-max", type=float, default=8.0, help="Maximaler Backoff in Sekunden")
    parser.add_argument("--no-api-check", action="store_true", help="API-Check für Adobe/RSS vor Ingestion deaktivieren")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else None
    connector_config = ConnectorConfig(
        timeout_seconds=args.timeout,
        max_retries=max(args.retries, 0),
        backoff_seconds=max(args.backoff, 0.0),
        backoff_factor=max(args.backoff_factor, 1.0),
        max_backoff_seconds=max(args.backoff_max, 0.0),
    )
    data = run_mvp(
        adobe_file=args.adobe,
        rss_file=args.rss,
        home_file=Path(args.home),
        out_file=out_path,
        connector_config=connector_config,
        api_check=not args.no_api_check,
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
