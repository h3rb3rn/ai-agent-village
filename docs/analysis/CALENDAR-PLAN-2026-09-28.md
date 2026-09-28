# AI Village Kalender – Plan

**Auslöser (Betreiber, 2026-09-28, sinngemäß zusammengefasst):** Die
Bewohner sollen ihren eigenen Tagesablauf und ihre Woche selbst planen,
solange keine externen Anforderungen (zugewiesene Aufgaben, Meetings,
Kollaborations-Checkpoints, Forschungsvorschläge) sie zwingen, ihren
Kalender anzupassen. Der Betreiber möchte die Kalender aller Bewohner
**inklusive König** im Dashboard einsehen können. Tageszeitung (Gazette)
und Jour Fixe sollen als **feste, wiederkehrende Bestandteile** in jedem
Kalender erscheinen. Nachtrag: Die Planung soll an den **bisher
gemessenen Inferenzgeschwindigkeiten und Token-Outputs** gemessen und
prognostiziert werden – kein aspirationaler Plan, sondern einer, der zur
tatsächlichen Kapazität des jeweiligen Agenten passt.

## Bezug zu bestehender Infrastruktur

- **Jour Fixe** existiert bereits (`village/meetings.py`, `MeetingStore`) –
  wiederkehrende Meeting-Instanzen, Berichtspflicht pro Bewohner.
- **Gazette** (P47–P54) liefert bereits das Muster für begrenzte,
  formatarme Freitext-Beiträge und einen dauerhaften, sich selbst
  auflösenden Kontext-Hinweis statt Einzelnachrichten – dasselbe Muster
  ist für die Kalenderplanung direkt wiederverwendbar.
- **Kapazitätsmessung**: `village/lifecycle.py::InferenceTracker` führt
  pro Bewohner bereits eine `inference_requests`-Tabelle
  (`inference_lifecycle.sqlite3`) mit `created_at`, `completed_at`,
  `duration_ms`, `prompt_tokens`, `completion_tokens`, `state` – exakt die
  Rohdaten für eine Kapazitätsprognose (Zyklen/Stunde, durchschnittliche
  Tokenausgabe, durchschnittliche Zyklusdauer). Kein neues Messsystem
  nötig, nur eine Auswertung der bereits vorhandenen Historie.

## Entschiedene Designfragen (Betreiber, 2026-09-28)

- **Format:** Freitext-Tages-/Wochenplan, begrenzte Zeichenzahl (analog zu
  Gazette-Beiträgen), **keine** strukturierten Zeitslots mit
  Start-/Endzeit – vermeidet Konflikterkennung/Uhrzeit-Validierung, passt
  zum etablierten Formatarm-Prinzip bei kleinen Modellen (P30–P44).
- **Kapazitätsbezug:** Vor bzw. während der Planung sieht der Bewohner
  einen kurzen, faktenbasierten Kapazitätshinweis aus seiner eigenen
  Inferenzhistorie (z. B. "~X Zyklen/Stunde, durchschnittlich ~Y Tokens
  Ausgabe je Zyklus, zuletzt Z gemessene Anfragen") – der Plan soll sich
  daran orientieren, nicht daran vorbei planen.
- **Reihenfolge:** Erst dieses Plandokument, kein Code in diesem Zyklus –
  die laufende Gazette-Validierung (Kings Zuteilungsaktion, P54) wird
  zuerst abgeschlossen.

## Stufen (Entwurf, noch nicht final committed)

### Stufe 1 – Datenmodell + Kapazitätsprognose

- `village/calendar.py::CalendarStore` (wie `GazetteStore`,
  `coordination.sqlite3`): Tabellen `calendar_daily_plans` (agent, date,
  content, created_at, updated_at) und `calendar_weekly_plans` (agent,
  iso_week, content, created_at, updated_at). Je Bewohner + Zeitraum
  eindeutig (`UNIQUE(agent,date)` / `UNIQUE(agent,iso_week)`), erneutes
  Einreichen ersetzt statt zu duplizieren (bewährtes Gazette-Muster).
  Bewusst **kein** König-only-Gate – jeder Bewohner (inkl. King) plant
  für sich selbst, von Anfang an gleichberechtigt.
- Neue Kontextfunktion `Resident.capacity_forecast()` (analog
  `failure_tally()`, P46): liest `inference_lifecycle.sqlite3` read-only,
  berechnet über ein festes Zeitfenster (z. B. letzte 24h/letzte N
  abgeschlossene Anfragen) Zyklen/Stunde, Ø `completion_tokens`, Ø
  `duration_ms`; degradiert bei Fehlern zu `None`/leer, kein Blockieren
  des Zyklus. Gecacht wie `capabilities_summary`/`failure_tally`.
- Neue Aktion `calendar_operation` (`plan_day`, `plan_week`, `view`).
  `content` hart begrenzt (Vorschlag: 400 Zeichen wie Gazette-Beiträge).

### Stufe 2 – Feste wiederkehrende Einträge

- Gazette (täglich) und Jour Fixe (nach bestehender Taktung) erscheinen
  **automatisch, deterministisch** in jedem abgerufenen Kalender – nicht
  vom Bewohner selbst eingetragen, sondern serverseitig bei `view`
  zusammengeführt (analog wie der Auditor Ereignisse zusammenführt, kein
  LLM-generierter Text). Verhindert, dass ein Bewohner sie vergisst oder
  wegplant.
- Selbstgeplanter Freitext-Teil bleibt vollständig in der Hand des
  Bewohners; feste Einträge werden nur ergänzt, nie überschrieben.

### Stufe 3 – Dashboard-Sichtbarkeit (höhere Priorität als bei Gazette)

- Anders als bei der Gazette (PDF/Dashboard erst Stufe 4) ist
  Dashboard-Sichtbarkeit hier ausdrücklich ein Kernwunsch, kein
  Nachtrag – neuer Menüpunkt/Panel im Observatory-Dashboard, das alle 9
  Kalender (inkl. König) nebeneinander zeigt, inklusive der festen
  Einträge aus Stufe 2 und optional der Kapazitätsprognose als
  Kontextinfo je Bewohner.

## Bewusst noch offen / nicht entschieden

- Genaues Zeitfenster/Berechnungsdetail der Kapazitätsprognose (gleitend
  über 24h? letzte N Requests? beides anzeigen?).
- Ob/wie "externe Anforderungen zwingen zur Anpassung" technisch
  modelliert wird (reiner Hinweistext im Kontext vs. eine echte
  Verknüpfung zu offenen Tasks/Meetings/Checkpoints) – vermutlich
  zunächst nur textuelle Kontext-Anreicherung, keine harte Verknüpfung,
  um das Datenmodell schlank zu halten.
- Genaue Zeichenobergrenze und ob Tages- und Wochenplan unterschiedliche
  Grenzen brauchen (Wochenplan vermutlich etwas großzügiger).
