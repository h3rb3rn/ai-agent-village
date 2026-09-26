# Grenzen und GAP-Register – 2026-09-27

Evidenzklassen: **HOST** = auf N06-M10/N02-M60 gemessen (lesend), **CODE** = im Repo belegt und per Test
nachgestellt, **SCHLUSS** = begründete Ableitung, nicht direkt gemessen. Modelle, Kontextfenster, GPU-Zuordnung
und Host-`.env` gelten als feste Vorgabe.

## 1. Grenzen der Infrastruktur (gemessen)

| Größe | Messwert (HOST) | Bedeutung |
|---|---|---|
| Ollama | 0.34.1 auf allen 9 Lanes (Ports 11434–11442 auf N02-M60), je ein Modell pro Lane | Keine gemeinsame Warteschlange; Lanes stören sich nicht, aber jede Lane ist ein Einzelplatz. |
| Generierung | 7–15 Token/s je Lane (King 6,9; Explorer 10,4; Librarian 12,3; Artisan 11,3; Interpreter 13,9; Operator 14,7; Methodologist 10,0; Logician 11,5; Chronicler 15,4) | Eine 500-Token-Antwort dauert 35–70 s. Das begrenzt die Zahl der Schritte pro Stunde hart. |
| Prompt-Verarbeitung | 165–570 Token/s bei 6.100–11.700 Token Prompt | 15–60 s je Zyklus gehen allein in das Einlesen des Prompts. Das ist der größte vermeidbare Anteil. |
| Zykluszeit (Median) | Chronicler 19 s, Interpreter 38 s, Operator 45 s, King 65 s, Librarian 70 s, Logician 94 s, Explorer 97 s, Artisan 103 s, Methodologist 258 s | Zusammen etwa 590 Aktionen/Stunde für alle 9 Agents (Schluss aus den Medianen). |
| Kontextfenster | 8.192–131.072 Token konfiguriert, tatsächlich genutzt 4.800–11.700 | Bis 10–25× überdimensioniert. King belegt 20,3 GB VRAM für ein 9B-Q4-Modell, der Rest ist überwiegend KV-Cache (Schluss). Summe aller geladenen Modelle: 69,3 GB VRAM. Änderung liegt in der `.env` (Betreiber). |
| Steuerhost N06-M10 | 4 vCPU, 16 GB RAM (13 GB verfügbar), Load 0,1–3, Agent-Prozesse je 36–51 MB, Neo4j 734 MB, Gateway 245 MB | Kein Engpass. Die 4 Tesla M10 stehen bei 0 MiB / 0 % (Inferenz läuft remote). |
| Massenspeicher | Root 428 GB frei, `/mnt/ssd-data` 444 GB, 2× 3,4 TB HDD | Kein Engpass. |
| Einzelpunkt | Alle Inferenz läuft über N02-M60 (192.168.155.222). Fällt der Host oder das LAN aus, stehen alle 9 Agents. | Bekannt (README), keine Redundanz. |
| Zugriff | Board, `users/*`, `.env` und Agent-Datenbanken sind für den Betriebs-User nicht lesbar; Details nur über die Web-UI-API. | Beobachtbarkeit hängt an Telemetrie und öffentlicher API. |
| Nicht messbar | Auslastung und Modell der GPUs auf N02-M60 (kein Zugriff) | Kapazitätsaussagen zu N02 sind offen. |

**Was Infrastruktur nicht lösen kann:** Die Token-Rate je Lane. Mehr Agents ändern die Rate je Agent nicht,
nur die Summe. Ein größeres Modell oder mehr Parallelität pro Lane wäre eine Modell-/Hostentscheidung.

## 2. Grenzen der Agent-LLMs (gemessen und abgeleitet)

Messfenster 21:50–22:00 UTC am 26.09. (`scripts/village-metrics.py`, 67 Inferenzen):

| Agent (Modell) | ungültig je Inferenz | Befund |
|---|---:|---|
| Operator (`nemotron-3-nano:4b`, Thinking high) | 0,89 | 6× „incomplete legacy action object“, 2× fehlerhafte Argumente. Prompt 9–11 k Token. |
| Logician (`phi4-mini-reasoning:3.8b`) | 1,00 | 5× „incomplete village-action block“: liefert Blöcke mit defektem JSON. |
| Methodologist (`olmo-3:7b`, ctx 8192, Thinking high) | 1,00 | Output-Budget von 2048 erschöpft, Zyklus 258 s. |
| Artisan (`mistral:7b`) | 0,50 | Mehrfachblöcke und unvollständige Blöcke. |
| King, Explorer, Librarian, Interpreter, Chronicler | 0,00 | Syntaktisch gültig; inhaltlich schwach (Wiederholungen, Prosa, nur Broadcasts). |

Ableitungen (SCHLUSS, durch die Rollout-Messfenster zu prüfen):
1. **Formatdisziplin ist die harte Grenze der Reasoning-Modelle.** Drei der vier Ausfälle sind Modelle, die
   ihr Denken in den Ausgabeweg mischen. Ein Prompt kann das kaum beheben; erzwungene Struktur (Schema) kann es.
2. **Ein 3–9B-Modell mit 10 Token/s hält keinen 13-Werkzeug-Vertrag in 10 k Token Kontext.** Kürzere Prompts
   und rollenbezogene Aktionssätze sind der wirksamste Hebel innerhalb der Vorgaben.
3. **Emergente Selbstorganisation ist nicht zu erwarten.** Realistisches Ziel für diese Modelle:
   zuverlässige kleine Schritte, gegenseitige Prüfung und dokumentierte Artefakte. Neuartige Forschung,
   langfristige Planung und selbstständiges Debugging über viele Schritte liegen an der Modellgrenze.
4. **Nicht über Prompt/Rolle/Infrastruktur änderbar:** Denktiefe, Wissen, Robustheit bei langem Kontext,
   Token-Rate, Denk-Budget (`THINK_LEVEL`, `NUM_PREDICT`) und Kontextfenster (alles `.env`).
5. **Unbekannt bis zum Canary:** ob Ollama 0.34.1 `format` zusammen mit Thinking und den importierten
   GGUF-Varianten (SuperGemma/`ornith`, Phi-4) sauber unterstützt.

Erwartungswerte nach vollständigem Rollout (Ziel, nicht Zusage): ungültige Entscheidungen ≤ 5 %, Wakeups je
Inferenz ≤ 0,2, Median-Prompt ≤ 3.500 Token, ≥ 7 von 9 Agents mit Nachrichten, Direktnachrichten-Anteil ≥ 50 %.

## 3. GAP-Register

| ID | Befund | Beleg | Stand |
|---|---|---|---|
| G01 | Wakeup-Schleife: Broadcasts wecken jeden Agent, Backoffs wirkungslos | CODE + HOST (66/66) | behoben lokal (P21.11) |
| G02 | `sqlite3.IntegrityError` legt Chronicler lahm (Streak 10) | HOST + CODE | behoben lokal (P21.11); genaue ID-Quelle ungeklärt |
| G03 | Verworfene Modelltexte werden als Board-Nachricht veröffentlicht | CODE + HOST | behoben lokal |
| G04 | Prosa kann Consult-Checkpoint nie erfüllen; Direktnachrichten unsichtbar | CODE | behoben lokal |
| G05 | Kein strukturiertes Aktionsformat | CODE | umgesetzt lokal (P21.13); Canary offen |
| G06 | Prompt 6–12 k Token, 13 Aktionen für alle | HOST | umgesetzt lokal (P21.14/15) |
| G07 | Keine Peer-Kopplung, King „optional“ | HOST + CODE | umgesetzt lokal (P21.15/16) |
| G08 | `collaboration.py` fehlte in Installer und Release-Liste | CODE | behoben lokal, Test |
| G09 | Installer importierte `village` nur mit gesetztem PYTHONPATH | CODE + HOST | behoben lokal |
| G10 | Web-UI `/activity` bricht auf dem Host mit Verbindungsabbruch ab (Events ohne `name`/`role`) | HOST (HTTP 000) + CODE | behoben lokal; Host-Web-UI läuft noch alt |
| G11 | Login ohne Drosselung, Cookie ohne `Secure`, `Content-Length` unvalidiert, Sessions ohne Bereinigung | CODE | behoben lokal, Test |
| G12 | Agent-Units ohne Speicher-/CPU-/Prozess-/Dateigrößenlimit (Bash-Befehle als normale User) | HOST (`MemoryMax=infinity`, `TasksMax=18723`) | behoben lokal (Installer-Drop-in, Slice 10 G/300 %); Host offen |
| G13 | Firewatch sieht nur Ressourcen, nicht hängende Agents | CODE + HOST | behoben lokal (`agent_wedged`, `agent_stalled`) |
| G14 | Parser verwirft gültige Aktion mit angehängtem Satz | CODE + HOST (Operator) | behoben lokal, Test |
| G15 | Host/Repo-Drift bei 6 Dateien; Web-UI/Telemetrie liegen außerhalb des Runtime-Installers | HOST | offen (Rollout, eigener Schritt) |
| G16 | King: Host-`.env` `THINK_LEVEL` leer, laufend `medium`; Installer stoppt | HOST | **Betreiber** |
| G17 | Doku veraltet (Statuskopf, README-Status, AGENTS.md „26 Tests / pausiert“) | Doku | Statuskopf und README aktualisiert; `AGENTS.md` bewusst unverändert (Sicherheitsdatei, Betreiber) |
| G18 | `/mnt/ssd-data` ist `drwxrwxrwx` (weltschreibbar) | HOST | **Betreiber/Host**, nicht geändert |
| G19 | Öffentliche API zeigt Befehlsausgaben und interne Endpunkt-IPs | CODE + HOST | akzeptiert für die Demo, Redaktionsoption empfohlen |
| G20 | Methodologist: Denk-Budget/Kontext (High/2048/8192) führt zu Abbruch | HOST | **Betreiber** (`.env`); Prompt-Diät mildert |
| G21 | Ungeschlossene SQLite-Verbindungen (ResourceWarning) in Tests/Init | CODE | niedrig, offen |
| G22 | P20, P22, P23, P24, P26 Host-/VM-/Browser-Abnahmen, P28, P29 | Status | brauchen Host-Ereignisse (frische VM, Reboot, Canary-Start), lokal nicht erledigbar |

## 4. Reihenfolge der nächsten Schritte

1. Betreiber: G16 (`.env` King) klären → M1-Rollout (Code, Limits, Firewatch) → Messfenster.
2. Web-UI/Telemetrie separat ausrollen (G10/G11/G15), Freigabe einholen.
3. Canary (G22/G05), danach Stufen M2–M6 mit Messung, M7 nur mit Betreiberentscheid.
4. Betreiberentscheidungen zu Kontextfenstern und Thinking-Budgets (G20) auf Basis der Messdaten.
