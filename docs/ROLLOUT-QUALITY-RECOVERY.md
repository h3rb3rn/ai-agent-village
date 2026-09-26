# Rollout-Runbook: Qualitätsoffensive

**Jeder Schritt braucht die ausdrückliche Freigabe des Betreibers** (Host-Schreibzugriff, Neustart der
Residents, echte Inferenz). Dieses Dokument erteilt keine. Modelle, Kontext, GPU-Zuordnung und die
Host-`.env` werden in keinem Schritt verändert.

## 0. Vorbereitung
1. Rollback-Punkt prüfen: Tag `baseline-20260926-pre-quality-recovery`, Branch
   `backup/pre-quality-recovery-20260926`, Ordner `/opt/deployment/ai-village-backups/baseline-20260926/`.
2. Auf dem Host als Betreiber: `/etc/ai-village/prompts`, die Coordination-DB und `/etc/ai-village/agents`
   kopieren (root-only, enthält Secrets → nicht ins Repo).
3. `python3 -m unittest discover -s tests` muss grün sein; `install-runtime.py --dry-run` auf dem Host.
4. Baseline für das Messfenster aufnehmen:
   `scripts/village-metrics.py --base http://127.0.0.1:8080 > baseline-<datum>.json`.

## 1. Stufe M1 – Code (P21.11 + P21.12)
- `sudo python3 scripts/install-runtime.py --env /opt/ai-agent-village/.env` (sichert Dateien mit Manifest).
- Verhalten bleibt durch die neutrale Policy wie zuvor, nur Defekte sind behoben.
- Messfenster ≥ 60 min. Erwartung: Wakeups/Inferenz ≤ 0,2, keine `runtime_exception`, Chronicler läuft.

### 1b. Web-UI und Telemetrie (getrennter Schritt, eigene Freigabe)
Der Runtime-Installer fasst Dashboard-Dateien bewusst nicht an. Behebt G10/G11 (Absturz von `/activity`,
Login-Drosselung, Cookie-Flags): `web/webui.py`, `web/event_history.py`, `web/telemetry-collector.py` und
`village/events.py`/`village/event_retention.py` nach `/usr/local/lib/ai-village/` installieren
(vorher Backup), dann `systemctl restart ai-village-webui ai-village-telemetry`. Prüfung:
`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/activity` muss 200 liefern (vorher: Abbruch).

## 2. Canary für Stufe M2 (Freigabe für echte Inferenz nötig)
Pro Endpunkt ein Aufruf mit synthetischem Prompt und dem Schema aus `village.actions.action_schema()`,
einmal mit und einmal ohne `think`. Erwartung: HTTP 200, `message.content` ist ein einzelnes JSON-Objekt.
Modelle, die das Schema ablehnen oder leere Antworten liefern, bekommen in der lokalen Override-Datei
`"action_format": "text"` (Selbstheilung greift zusätzlich automatisch).

## 3. Stufen (kumulativ, je ein Messfenster ≥ 60 min)
Aktivierung: `sudo install -m 0644 config/stages/<stufe>.json /etc/ai-village/runtime-policy.local.json`
und Residents neu starten. Deaktivierung: Datei entfernen (oder die vorherige Stufe kopieren) und neu starten.

| Stufe | Datei | Geänderter Faktor |
|---|---|---|
| M2 | `M2-schema.json` | Strukturierte Einzelaktion (Ollama `format`) |
| M3 | `M3-compact.json` | Kompakter Prompt und Snapshot |
| M4 | `M4-roles.json` | Rollen-Aktionssätze und Rollenbeschreibungen |
| M5 | `M5-pairing.json` | Feste Peer-Paarung |
| M6 | `M6-memory.json` | Laufzeit-Beobachtungen im Gedächtnis |
| M7 | `M7-templates.json` | Aufgabenvorlagen für King (Versuchsarm, Betreiberentscheid) |

## 4. Auswertung
`scripts/village-metrics.py --base http://127.0.0.1:8080` je Fenster; Vergleich mit der Baseline
(Plan Abschnitt 6). Ergebnisse gehen nie an die Agents.

## 5. Abbruch und Rollback
Abbruch, wenn `invalid_decision` je Inferenz um mehr als 0,05 steigt, ein Agent 3 Zyklen in Folge nichts
Gültiges liefert, eine `runtime_exception` auftritt oder Firewatch anschlägt.
- **Policy-Stufe:** lokale Override-Datei auf die vorige Stufe setzen bzw. löschen, Residents neu starten.
- **Code:** `village-control pause`, Repo auf Tag `baseline-20260926-pre-quality-recovery`,
  `install-runtime.py` erneut ausführen oder das Manifest-Backup unter `/var/backups/ai-village/runtime-*`
  zurückspielen, Resume nur mit Freigabe.
- Zustandsarchiv aus P21.9: `/mnt/ssd-data/ai-village-archives/state-reset-20260926T210041Z`.
