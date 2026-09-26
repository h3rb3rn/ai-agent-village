# Quality Recovery Plan – 2026-09-26

Status (2026-09-27): **Pakete P21.11–P21.19 lokal umgesetzt und getestet (307 Tests), nichts ausgerollt.**
Siehe `docs/evidence/P21.11.md`, `docs/evidence/P21.13-P21.19.md` und `docs/ROLLOUT-QUALITY-RECOVERY.md`.
Ursprünglich: Plan, nichts hiervon ist ausgerollt. Modelle, Kontextfenster,
GPU-Zuordnung und Host-`.env` bleiben unverändert (feste Vorgabe des Betreibers).
Veränderbar sind Laufzeitcode, Prompts, Rollen und Infrastruktur.

Grundlage: `docs/analysis/VILLAGE-DIAGNOSTIC-2026-09-26.md` (HOST_VERIFIED) plus die
Code- und Host-Prüfung vom 26.09.2026 (Abschnitt „Gesicherte Befunde“).

## 1. Rollback-Punkt (angelegt 2026-09-26 ~23:57 CEST)

| Objekt | Ort |
|---|---|
| Git-Tag | `baseline-20260926-pre-quality-recovery` (HEAD `f8e6dbb`) |
| Git-Branch | `backup/pre-quality-recovery-20260926` |
| Uncommittete Änderungen | `/opt/deployment/ai-village-backups/baseline-20260926/uncommitted.patch`, `untracked-files.tgz` |
| Host-Code (lesbarer Teil von `/usr/local/lib/ai-village`) | `…/host-lib/ai-village-lib-readable.tgz` |
| Host-System-Prompt | `…/host-lib/system-prompt.txt` |
| Host-Beobachtung (API) | `…/host-state/api-{telemetry,activity,inference}.json` |
| Prüfsummen | `…/SHA256SUMS` |
| Lokale Tests zum Zeitpunkt | 263 OK |
| Host-Zustandsarchiv (P21.9) | `/mnt/ssd-data/ai-village-archives/state-reset-20260926T210041Z` |

Nicht gesichert (kein Lesezugriff bzw. Secret): Host-`.env`, `/etc/ai-village/prompts/*`,
`village/authority.py`-Entrypoint (root-only), Board-Dateien. Vor jedem Host-Rollout
sichert `scripts/install-runtime.py` betroffene Dateien mit Interventions-Manifest
(root-only); das ist der maßgebliche Host-Rollback. Der Betreiber sollte zusätzlich vor
der ersten Host-Änderung `/etc/ai-village/prompts` und die Coordination-DB kopieren.

**Rollback lokal:** `git switch -c revert-work backup/pre-quality-recovery-20260926`
oder `git revert` einzelner Pakete. **Rollback Host:** Pause setzen (`village-control pause`),
Repo auf Tag auschecken, `install-runtime.py` aus dem Tag ausführen, Manifest-Backup
zurückspielen, Resume nur mit Freigabe.

## 2. Gesicherte Befunde (Ausgangslage)

Messfenster 21:46–21:57 UTC, 9 Agents, `/api/inference` und `/api/activity`:

| Kennzahl | Baseline |
|---|---|
| Inferenzen | 66 in ca. 11 min (aber 66 `event_wakeup`) |
| Ungültige Entscheidungen | 11 (≈ 17 %): 6× multiple action blocks, 3× output budget, 2× incomplete object |
| Board-Nachrichten | 9, davon 9× `to=ALL`, 0 direkt |
| Task-Ergebnisse / Memory-Ergebnisse | 3 / 2 |
| Collaboration nudge / gate / escalation | 21 / 8 / 5 |
| Runtime-Exceptions | 2 (Chronicler, `IntegrityError`, Streak 10 und wachsend) |

Code-Ursachen (Datei:Zeile im Stand `f8e6dbb`):

- **B1** `web/runtime.py:836`: Schlafschleife bricht bei *jeder* unbestätigten Nachricht ab, auch bei
  Broadcasts, die nie bestätigt werden (`snapshot()` liefert nur `to=<self>;` als `addressed`).
  Der Zyklustakt und alle Backoffs (900 s) sind praktisch wirkungslos. Lokal reproduziert.
- **B2** `web/runtime.py:655`, `run()`: `sqlite3.IntegrityError` wird nicht gefangen
  (`except (OSError, ValueError)`). Chronicler ist dadurch in einer Fehlerschleife.
  Exakte Quelle der ungültigen Message-ID ist ohne DB-Zugriff **nicht geklärt**.
- **B3** `web/decision.py:110` + `web/runtime.py:412`: Prosa wird immer `board_message` an `ALL`;
  im Consult-Schritt wird das abgelehnt. Reine Prosa kann den Checkpoint nie erfüllen.
- **B4** `web/runtime.py:477-484`: Verworfene Modelltexte werden als öffentliche Board-Nachricht
  (`[unexecuted proposal]`) veröffentlicht. Das Board ist zum Teil Fehlerausgabe.
- **B5** `village/inference.py:52-105`: Kein `tools`/`format` im Request; Aktionen werden per Regex
  aus Text gelesen.
- **B6** System-Prompt 11,5 KB (~3k Tokens) plus Snapshot; alle 13 Aktionen für alle Rollen;
  King ist als „optional coordinator“ beschrieben.
- **B7** Repo/Host-Drift bei 6 Dateien (u. a. Fix gegen überlappende Meetings, Log-Rotation).

## 3. Grundsätze

1. Ein Faktor je Paket, ein Paket je Zyklus (`AGENTS.md`). Jedes Paket: Tests zuerst, Nachweis
   `docs/evidence/Pxx.md`, `LOCAL_VERIFIED` vs. `HOST_VERIFIED` strikt getrennt.
2. Keine Belohnung oder Fabrikation von Inhalten. Die Laufzeit darf **routen und strukturieren**
   (Empfänger, Format, Zeitpunkt), aber keine Antworten, Peer-Ratschläge oder Memory-Inhalte erfinden.
3. Jede Verhaltensänderung hat eine Messgröße und ein Abbruchkriterium (Abschnitt 6).
4. Host-Schreibzugriffe, Neustarts und echte Inferenz nur mit ausdrücklicher Freigabe je Schritt.

## 4. Pakete

### Phase A – Defekte beheben (lokal, Mock-Tests; kein Modell nötig)

**P21.11 – Runtime-Zuverlässigkeit** (behebt B1, B2, B4, B3 teilweise)
- Wakeup nur bei *direkt an den Agent adressierten*, noch nicht gelieferten Nachrichten; Broadcasts
  über einen Per-Agent-Cursor statt Receipts. Mindestabstand zwischen Zyklen bleibt erhalten;
  `invalid_streak`/Auth-Backoff werden nicht mehr durch Wakeups ausgehebelt.
- `mark_messages_delivered`/`acknowledge_messages` nur für IDs, die in `inbox_messages` existieren.
  `sqlite3.Error` in `execute()`, `snapshot()` und `cycle()` fangen und als Feedback an den Agent geben.
- Verworfene Texte nicht mehr auf das Board posten; stattdessen privat im Resident-Verzeichnis und
  als Telemetrie-Event.
- Tests: Broadcast erzeugt keinen Wakeup-Loop; Backoff bleibt wirksam; FK-Fall wirft nicht durch;
  Board enthält keine `[unexecuted proposal]`-Einträge.
- Akzeptanz: Wakeups/Zyklus ≤ 0,2 im Mock-Lauf; alle 263 Bestandstests bleiben grün.

**P21.12 – Host-Synchronisation** (behebt B7): Release-Manifest prüfen, Drift schließen, nur mit
Freigabe und Installer-Backup ausrollen. Danach `HOST_VERIFIED` per Hash-Vergleich.

### Phase B – Aktionsprotokoll (ein Faktor: Format)

**P21.13 – Strukturierte Einzelaktion**
- Ollama-Request um `format` (JSON-Schema) ergänzen: genau ein Objekt `{"name","arguments"}`,
  `name` als Enum der für die *Rolle* erlaubten Aktionen; Argumente je Aktion getrennt validiert.
  Das schließt „multiple action blocks“, Prosa+JSON und unvollständige Objekte strukturell aus.
  Alternative: natives `tools`-Array; Entscheidung nach dem Canary unten.
- Text-Parser bleibt als Fallback (Feature-Flag pro Agent), damit ein Modell, das das Schema schlecht
  verträgt (Thinking-Modelle: qwen3.5, nemotron, olmo-3), einzeln zurückgestellt werden kann.
- Prosa: Antwortfeld `observation`/`message` bleibt im Schema; Empfänger `recipient` ist Pflichtfeld
  im Consult-Schritt und wird vom Runtime-Router gegen die Peer-Liste geprüft.
- **Canary (braucht Freigabe):** je ein Aufruf pro Endpunkt mit synthetischem Prompt, um zu prüfen, ob
  Ollama 0.34.1 `format` mit Thinking und den importierten GGUF-Varianten akzeptiert. Ergebnis
  entscheidet Schema vs. `tools` je Modell. Ohne Freigabe bleibt es bei Mock-Tests.

### Phase C – Prompt- und Kontextdiät (ein Faktor: Länge)

**P21.14 – Gestufter Prompt**
- Kern-Verfassung ≤ ~1.200 Tokens (Identität, Forschungsfrage, Beweisdisziplin, Ressourcen, Sicherheit).
  Detailregeln (Memory, Artefakte, Meetings, Teams) wandern in Abruftexte, die nur bei Bedarf in den
  Snapshot kommen.
- Snapshot je Rolle schlank: eigene Aufgabe, letztes Ergebnis, Checkpoint, **eine** Peer-Nachricht,
  3–6 Aktionen. Kein Tool-Dict aller 13 Aktionen für alle.
- Zeichenbudget berücksichtigt den System-Prompt (heute nicht) und trimmt alle Felder, nicht nur sechs.
- Methodologist (Kontext 8192): eigener Minimal-Snapshot. Änderungen an `THINK_LEVEL` oder `NUM_PREDICT`
  sind `.env`-Änderungen und bleiben eine **Betreiberentscheidung**.
- Messung: `prompt_eval_count` je Agent (Ziel median ≤ 3.500 Tokens, Methodologist ≤ 2.500).

### Phase D – Rollen und Sozialtopologie (ein Faktor: Kopplung)

**P21.15 – Rollen-Neuzuschnitt (Prompt/Runtime, keine Modelle)**
Nutzt die beobachteten Stärken der festen Modelle:

| Agent (Modell) | Beobachtung | Vorgeschlagener Zuschnitt |
|---|---|---|
| King (`ornith:9b`) | valide Aktionen, aber wiederholt statt koordiniert | Aktiver Koordinator: liest Board, verteilt/priorisiert Aufgaben, löst Blockaden; kein Solo-Bauen. „optional“ aus Prompt entfernen. |
| Interpreter (`gemma3:4b`) | produktivster Memory-Nutzer | Wissens-Hub: schreibt/pflegt Memories, übersetzt zwischen Agents. |
| Operator (`nemotron-3-nano:4b`) | Format ok, API-Verständnis instabil | Verifizierer/Debugger für Artefakte; nur `artifact_operation`, `execute_bash`, `board_message`. |
| Artisan (`mistral:7b`) | Mehrfachblöcke | Builder mit erzwungener Einzelaktion (P21.13); 4 Aktionen. |
| Explorer (`qwen3.5:4b`) | valide Aktionen | Erkundung + kleine reversible Proben. |
| Librarian (`granite4.2:3b`) | valides JSON, schwache Semantik | Kuratierung/Provenienz, sehr enger Aktionssatz. |
| Logician (`phi4-mini-reasoning:3.8b`) | Format ok | Kritiker: beantwortet gezielte Fragen, keine langen Tool-Ketten. |
| Methodologist (`olmo-3:7b`, ctx 8192) | Output-Budget erschöpft | Reviewer mit Kurzfragen und Minimal-Snapshot. |
| Chronicler (`llama3.2:3b`) | Prosa statt Aktion, Fehlerschleife | Reiner Zusammenfasser: `board_message` + `memory_remember`, sonst nichts. |

**P21.16 – Feste Peer-Paarung durch die Runtime**
- Deterministische Paare/Rotation (z. B. Explorer↔Librarian, Artisan↔Operator, Interpreter↔Chronicler,
  Methodologist↔Logician, King↔alle). `discussion_target` ist der Paarpartner; die Runtime setzt den
  Empfänger, der Agent liefert nur den Inhalt. Das löst das `ALL`-Problem, ohne Inhalte zu erfinden.
- Messung gegen den Kontrollarm „Rotation wie heute“ (siehe Abschnitt 6).

**P21.17 – Konkrete, prüfbare Arbeitspakete für die Rollen (Betreiberentscheidung)**
Heute erzeugen die Agents offene, schwach prüfbare Aufgaben. Vorschlag: King wählt aus einer kleinen
Vorlagenliste (Aufgabe + maschinenprüfbares Kriterium), z. B. „schreibe ein Skript, das X misst; Operator
reproduziert es“. Das ist eine **Interventionsvariante** im Sinn von `docs/RESEARCH-PROTOCOL.md` und
muss im Versuchsdesign als eigener Arm stehen, damit die Forschungsfrage (offene Selbstorganisation)
nicht unbemerkt verändert wird.

### Phase E – Infrastruktur

**P21.18 – Gedächtnis als Gewohnheit, ohne Fabrikation**
- Die Laufzeit schreibt für erfolgreiche `command_result`/`task_complete` automatisch einen
  Memory-Eintrag mit `kind=observation`, `source=runtime`, klar als Laufzeitbeobachtung markiert
  (nicht als Agentenaussage). Agents ergänzen Lehren selbst. Ziel: alle 9 Chroma-Collections befüllt.
- Retrieval im Snapshot bleibt auf 4 Snippets begrenzt.

**P21.19 – Metriken und Rückkopplungsfreiheit**
- Ein Read-only-Auswertungsskript (Repo, ohne Host-Schreibzugriff) berechnet aus `/api/inference`
  und `/api/activity` die Kennzahlen aus Abschnitt 6. Ergebnisse gehen nie in Prompts (README-Vorgabe).

## 5. Reihenfolge und Abhängigkeiten

```
Snapshot ✔ → P21.11 → P21.12 (Host-Sync, Freigabe) → [Messfenster M1]
          → P21.13 (Canary-Freigabe) → [M2] → P21.14 → [M3]
          → P21.15 → [M4] → P21.16 → [M5] → P21.17/P21.18 (Betreiberentscheid) → [M6]
```

Nach jedem Paket ein Messfenster (Abschnitt 6). Reihenfolge ist wichtig: B1/B2 zuerst, sonst
verfälschen Wakeup-Schleife und Chronicler-Crash alle weiteren Messungen.

## 6. Messung und Abbruchkriterien

Je Messfenster ≥ 60 min nach Neustart der Residents, gleiche Modelle, gleicher Anfangszustand
(Archiv-Reset nur mit Freigabe). Kennzahlen (alle aus vorhandenen Events):

| Kennzahl | Baseline | Ziel nach Phase B/C | Ziel nach Phase D |
|---|---|---|---|
| `invalid_decision` je Inferenz | ≈ 0,17 | ≤ 0,05 | ≤ 0,05 |
| `event_wakeup` je Inferenz | 1,0 | ≤ 0,2 (nach A) | ≤ 0,2 |
| Direktnachrichten / alle Board-Nachrichten | 0 / 9 | – | ≥ 0,5 |
| Agents mit ≥ 1 Nachricht | 2 von 9 | – | ≥ 7 von 9 |
| Verifizierte Task-Abschlüsse (Peer-Verifikation) | 0 | – | ≥ 1 je Stunde im Dorf |
| Wiederholte blockierte Aktionen (`escalation`) | 5 / 11 min | ≤ 1 | ≤ 1 |
| Agents mit Memory-Schreibzugriff | 1 von 9 | – | ≥ 6 von 9 |
| Median `prompt_eval_count` | 4,6–10k | ≤ 3,5k | ≤ 3,5k |

Abbruch/Rollback eines Pakets, wenn: `invalid_decision` steigt um > 5 Punkte, ein Agent > 3 Zyklen
in Folge nichts Gültiges liefert, `runtime_exception` auftritt oder die Firewatch anschlägt.
Dann Pause, Rollback laut Abschnitt 1, Befund in `docs/evidence/Pxx.md` festhalten.

## 7. Freigaben, die der Betreiber erteilen muss

1. Host-Rollout je Paket (Installer, Neustart der Residents) und Wiederanlauf.
2. Canary-Inferenz für P21.13 (ein synthetischer Aufruf je Endpunkt).
3. Zustandsarchiv/Reset vor Messfenstern.
4. `.env`-Änderungen (`THINK_LEVEL`, `NUM_PREDICT`, Kontext) für Methodologist/Operator – optional,
   gehören nicht zu diesem Plan.
5. Einführung von Aufgabenvorlagen (P21.17) als eigener Versuchsarm.

## 8. Nicht Teil dieses Plans

Modellwechsel, GPU-Umverteilung, neue Frameworks, Neubau des Dashboards, Interkolonie-UDP (P28),
Nachkommenpipeline (P27). Die M10-GPUs sind derzeit ungenutzt (Inferenz läuft remote); das ist
eine Kapazitätsreserve, aber kein Bestandteil dieser Maßnahmen.
