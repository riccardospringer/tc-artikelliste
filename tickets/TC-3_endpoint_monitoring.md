# TC-3: Echtes Endpoint-Monitoring (Health-Metrik/Logging)

## Kontext
TC-2 liefert Live-Ingestion inklusive API-Preflight (Adobe/RSS) mit Retry/Backoff. Aktuell ist `/healthz` statisch (`status=ok`) und enthält keine operativen Laufzeitmetriken über die tatsächliche Erreichbarkeit der Live-Quellen.

## Ziel
Für Betrieb und Störungsanalyse sollen verwertbare Health- und Monitoring-Signale entstehen:
- Health-Ausgabe mit letzten Ingestion-Ergebnissen und Fehlerzustand.
- Strukturierte Logs für Preflight und Ingestion.
- Einfache Metriken (Counters/Latency), die sich von extern abfragen oder scrapen lassen.

## Scope (V1)
1. `run_mvp`/Preflight instrumentieren:
- Zeitmessung pro Connector (`adobe`, `rss`) und Gesamtzeit.
- Erfolgs-/Fehlzähler für API-Checks und Ingestion-Läufe.
2. Server-Endpunkte erweitern:
- `/healthz` um letzte Laufzeitinformationen ergänzen (`last_success_ts`, `last_error_ts`, `last_error_message`, `last_duration_ms`, `source_status`).
- Optionaler `/metrics`-Endpoint (textbasiert, Prometheus-kompatibel) für Basis-Counter und Durations.
3. Logging:
- Strukturierte Log-Zeilen (JSON) für Start, Erfolg, Fehler und Retry-Ereignisse.
- Korrelation über `request_id`/`run_id` in `load_articles`.
4. Tests:
- Unit-Tests für Health-State-Update bei Erfolg/Fehler.
- Unit-Tests für Metrics-Output (mindestens Presence/Format).

## Akzeptanzkriterien
1. Bei erfolgreicher Ingestion zeigt `/healthz` den letzten erfolgreichen Lauf mit Zeitstempel und Dauer.
2. Bei Fehlern zeigt `/healthz` den letzten Fehler samt Quelle (`adobe` oder `rss`) und bleibt HTTP 200, aber mit `status=degraded`.
3. Retry-Ereignisse sind als strukturierte Logs sichtbar und enthalten Versuchszahl und Delay.
4. Test-Suite enthält neue Monitoring-Tests und bleibt vollständig grün.

## Nicht im Scope
- Externe APM-Integration (Datadog/New Relic) in V1.
- Persistente Speicherung historischer Metriken über Prozess-Neustarts hinaus.
