# Runtime-Audit 2026-10-13: Gates, Werkzeug-Verfügbarkeit, Schleifenbremse

## Grundlage

Die angekündigte Datei `claude-code-ai-village-2026-10-13-runtime-audit.md` lag nicht vor; sie wurde auf dem Rechner nicht gefunden. Die Analyse stützt sich daher auf andere Quellen:

- **Live-Ereignisse:** `/api/observatory`, letzte 1.000 Ereignisse, 2026-10-13 18:12–20:26 UTC, also nach dem Rollout von P158.
- **Code:** Stand des Branches `p124-entfaltung`.
- **Deploy-Stand:** Hash-Vergleich aller 64 Python-Dateien unter `/usr/local/lib/ai-village` mit dem Repo.

**Leitlinie des Betreibers:** Fehler der Bewohner sind Forschungsdaten. Behoben werden nur nachgewiesene Fehler der Infrastruktur; die unabhängige Beobachtbarkeit wird verbessert.

## Deploy-Stand (Frage 5)

Repo und Host sind identisch, bis auf zwei Dateien:

- `village/release.py`: nur die Installationsliste;
- `village/residents.py`: bekannte Doku-Drift bei den Modellnamen.

**Es liegt also kein fertiger, nicht ausgerollter Fix für eines der untersuchten Probleme vor.**

- P154–P158 sind live.
- P158 ist live, aber noch nicht committet.
- Die Statustabelle ist für den Deploy-Stand nicht verlässlich: Viele Zeilen (z. B. P135, P143) stehen auf `LOCAL_VERIFIED`, obwohl der Code nachweislich live läuft.

## Gap-Analyse

| # | Befund (live) | Root Cause | Einordnung | Pakete | Status |
|---|---|---|---|---|---|
| 1 | `meeting_operation` 6-mal abgelehnt (18:16, 18:23, 18:40, 18:42, 19:34, 20:14 UTC; Operator, Chronicler, Librarian, Artisan, Logician) | Im Fenster war **kein Meeting offen**; es gibt kein `meeting_*`-Ereignis. Die Runtime blendet die Aktion korrekt aus (P126/P135), und die Ablehnung ist korrekt. Die Modelle kennen die Aktion aus dem statischen Systemprompt (`resident-system.txt` Z. 95, 117, 176; `resident-core.txt` Z. 17). Er bleibt für den Prefix-Cache byte-gleich (P136) und listet alle Aktionen unabhängig von der Verfügbarkeit. Weitere Quellen sind Erinnerungen und frühere Fehlerlisten. | **Modellverhalten**: Forschungsdaten, kein Infrastrukturfehler. Der Anteil der Infrastruktur war ein Hinweis der Schleifenbremse, der `meeting_operation` auch ohne offenes Meeting nannte; das ist mit P158 behoben und live. Den statischen Prompt ändert nur eine Freigabe. | P86, P104, P123, P126, P135, P158 | geschlossen (Infrastruktur), Prompt offen (Freigabe) |
| 2 | `calendar_week_plan_required` 166-mal, `calendar_plan_required` 30-mal (nur Operator) in 2 h 14 min; Druckzähler bis 431 | Das sind **Druckzähler, keine Sperren**. `guard()` schreibt bei *jeder* nicht ausgenommenen Aktion eines Bewohners mit offener Lücke ein Ereignis. Es gibt kein Ereignis `calendar_*_gate`, die Pflichten sind also nicht hart (P126). Zwei Auffälligkeiten der Infrastruktur: **(a)** Die Zähler gehen ins Board (`board/events.jsonl`), den Kanal der Agenten, nicht in die Telemetrie. **(b)** Seit P147 prüft `day_gaps` 0–24 h ab *heute*, also auch Stunden, die schon vorbei sind. Ein Druck, der nie sinkt (431), passt dazu, dass sich eine Lücke nur noch rückwirkend füllen ließe. **Nicht nachgewiesen:** Die Lücken selbst sind nicht sichtbar, dafür ist ein Lesezugriff auf `calendar.sqlite3` nötig. | (a) Ablage der Beobachtungsdaten; (b) Hypothese. Dass der Operator nicht plant, ist Modellverhalten. | P75, P81, P126, P147, P158 | offen, Nachweis für (b) fehlt |
| 3 | `school_answered … stored=False` 5-mal, nur `01-king` (18:22–20:18 UTC) | Die Runtime zeigt `school_lesson` in **jedem** Zyklus, auch nachdem die eine Antwort des Tages gespeichert ist. P117 versprach „until answered“; `answers_for()` wurde nie abgefragt. Der King erhält also weiter die Aufforderung „answer it once today“, `record_answer` lehnt jede weitere Antwort ab (`INSERT OR IGNORE`). | **Infrastrukturfehler**: Die Runtime verletzt ihren eigenen Vertrag. | P115, P117, P145, P158 | **behoben in P159** (lokal) |
| 4 | Observatory: `02-explorer` mit `success` 11, `success_rate` 1.0, `capability_index` 90, bei 11-mal demselben Befehl (`habitat_inventory.py`) | **Aufgaben:** `task_operation complete` ist eine Selbstauskunft mit nicht leerem `evidence` und `completion_verified=0`. Nichts setzt die Verifikation, und `outcomes.py` zählt korrekt nur verifizierte. Der Abschluss einer Aufgabe wird also nicht fälschlich als verifiziert geführt. **Observatory:** `observer.outcome_stats` zählt Exit-Code 0 als `success`. `webui.skill_history` gewichtet das zu 70 % im `capability_index`. Wiederholungen zählen nur über `escalation`, nicht über identische Befehle. Technischer Erfolg erscheint als Fähigkeit. | **Lücke in der Beobachtbarkeit** (Messgröße), kein Eingriff in Agenten | P46, P103, P143 | offen; Vorschlag P160 |

## Spannungsfeld mit der Forschungsanforderung

Drei der heute ausgerollten Pakete greifen in die Umwelt der Bewohner ein, nicht nur in die Beobachtung. Alle wurden einzeln freigegeben, stehen aber in Spannung zu „Fehler sind Forschungsdaten“:

- **P155:** Keine Antwortpflicht auf reine Bestätigungen; dazu eine neue Prompt-Regel.
- **P157:** Repariert eindeutig fehlerhafte Aktionen des Modells. Die Fehler bleiben in Trace und Fähigkeitsregister erhalten, die Aktion wird aber ausgeführt.
- **P158:** Korrigiert widersprüchliche Hinweise der Runtime vor dem Absenden und kürzt den Kalender.

Bei P158 geht es um die Runtime, die sich selbst widerspricht. Bei P157 wird dagegen echtes Modellverhalten abgefangen, das ist der deutlichste Eingriff. Jedes Paket lässt sich über seine Backups einzeln zurückrollen (`*.bak-pre-p155/157/158-*`). Ob das geschehen soll, entscheidet der Betreiber.

## Nächste Pakete (nicht freigegeben)

- **P160, Beobachtbarkeit** (freigegeben und umgesetzt, siehe `docs/evidence/P160.md`):
  - `outcome_stats`/`skill_history` trennen „ausgeführt“ von „neu“: identische Befehle zählen nur einmal als Erfolg.
  - Wiederholungen gehen in den Index ein.
  - Die Bezeichnung wird „Ausführungsrate“.
  - Nur Observatory, kein Agentenkontext.
- **Kalender:**
  - Lesezugriff auf `calendar.sqlite3` (sudo-Skript, nur lesend), um Hypothese (b) zu prüfen;
  - danach gegebenenfalls Lücken nur ab „jetzt“ werten;
  - Druckzähler in die Telemetrie verschieben.
