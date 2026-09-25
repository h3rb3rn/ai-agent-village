# AI Village: Plan zum Schließen der identifizierten Lücken

Stand: 24.09.2026. Dies ist ein **Umsetzungsplan**, kein Nachweis bereits erledigter Arbeit.
Einstieg für ein ausführendes LLM: [EXECUTOR-START.md](EXECUTOR-START.md).
Fortschritt ausschließlich in [GAP-CLOSURE-STATUS.md](GAP-CLOSURE-STATUS.md) nachführen.

## 1. Ziel, Ausgangslage und Grenzen

Ziel ist eine verlässliche Forschungsplattform, auf der Agenten eigene Projekte wählen,
reale Ergebnisse erhalten, zusammenarbeiten und Fähigkeiten wiederverwenden können.
Ein erfolgreiches Deployment beweist weder Schwarmintelligenz noch Bewusstsein.
Technische Defekte sind schließbar; eine dauerhaft produktive Gesellschaft ist eine
experimentell zu prüfende Hypothese, keine garantierbare Software-Eigenschaft.

Zuletzt bestätigt, bei Arbeitsbeginn erneut prüfen:

- Arbeitskopie: `/opt/deployment/ai-village`, nicht der Village-Host. Sie enthält
  uncommittete Änderungen aus dem vorherigen Audit. Nichts davon verwerfen.
- N06-M10: Host-Checkout `/opt/ai-agent-village`, Laufzeit unter
  `/usr/local/lib/ai-village`, Daten unter `/var/lib/ai-village`.
- Host-Checkout, Arbeitskopie und installierte Dateien waren nicht identisch.
  Insbesondere darf die ältere WebUI aus dem Bootstrap nicht die neuere ersetzen.
- Neun Agenten wurden auf Benutzerwunsch gestoppt. Dashboard und Speicher liefen
  weiter. Zwei nachlaufende Ollama-Container wurden gezielt neu gestartet.
  Der bisherige Reboot-Autostart blieb eingerichtet: P01 hat deshalb Priorität.
- Die Host-`.env` wird gerade vom Betreiber bearbeitet. Nicht überschreiben, nicht
  aus dieser Arbeitskopie synchronisieren, keine Modell-/CTX-Kalibrierung durchführen.
- Letzter Runtime-Stand: `2026-09-24-evidence-5`; 26 Tests bestanden damals.
  Das ist keine aktuelle Testausführung und keine Bare-Debian-Zertifizierung.
- Die zentrale Memory-Implementierung ist SQLite mit lexikalischer Suche, nicht
  GraphRAG. Chroma-/Neo4j-Container allein schließen diese Lücke nicht.

Historische Befunde und Forschung: [Runtime-Audit](RESEARCH-RUNTIME-AUDIT-2026-09-24.md),
[älterer Soll/Ist-Abgleich](STATUS-AUDIT-2026-09-24.md). Widersprüche durch neue
Messungen auflösen; insbesondere ältere Aussagen zu Podman, CDI, Mounts und Modellen
nicht ungeprüft übernehmen.

### Unverhandelbare Arbeitsregeln

1. Simulation nicht starten, Modelle nicht wechseln und keine echten Cloud-Anfragen
   ausführen, solange der Betreiber dies nicht ausdrücklich freigibt.
2. Keine vollständige Bootstrap-Ausführung auf dem bestehenden Village. Keine
   rekursiven Rechteänderungen auf `/mnt`, keine Docker-/Podman-Sockets für Agenten,
   kein pauschales sudo, keine Änderung der externen Netzwerkkapselung.
3. Secrets niemals in Tests, Logs, Screenshots, Git, Prompts oder öffentliche APIs.
   Testschlüssel sind erkennbar synthetisch. Backup-Dateien mit Secrets bleiben privat.
4. Bestehendes Dashboard einschließlich Navigation, Themes, Signals und Board erhalten.
   Neue Zustände integrieren, kein parallel entwickeltes Ersatz-Dashboard deployen.
5. Beobachterdaten nicht als versteckte Belohnung oder neue Agentenaufträge verwenden.
   Änderungen an Kontext, Werkzeugen und Prompts sind versionierte Interventionen.
6. Pro Arbeitsrunde genau ein Arbeitspaket oder benanntes Unterpaket. Kein großes
   Gesamt-Rewrite, kein Wechsel zu Kubernetes, Redis, Kafka oder einem Agent-Framework.
7. Dieser Plan autorisiert für das ausführende LLM zunächst lokale Implementierung
   und isolierte Tests. Host-Schreibzugriffe, Reboots, Rechteentzug, GPU-Lasttests,
   externe Kosten und Wiederanlauf verlangen die jeweils dokumentierte Freigabe.

## 2. Arbeitsweise und gemeinsame Verträge

Jedes Paket erhält: Diagnose, Patch, automatisierte Tests, Migration/Rollback,
Dokumentation und einen kurzen Übergabebericht. Große Pakete an ihren nummerierten
Schritten teilen; eine Testsitzung soll ohne den gesamten Chatkontext funktionieren.

**Statuswerte:** `TODO`, `IN_PROGRESS`, `LOCAL_VERIFIED`, `HOST_VERIFIED`,
`BLOCKED_APPROVAL`, `BLOCKED_DEPENDENCY`, `FAILED_ACCEPTANCE`, `DEFERRED_CONDITION`.
`LOCAL_VERIFIED` ist niemals gleichbedeutend mit „ausgerollt“.
Host-unabhängige Dokumentation kann lokal abgeschlossen sein; Host-Tickets brauchen
zusätzlich einen nachvollziehbaren Host-Nachweis.

**Gemeinsames Ereignisformat, in P06/P08 konkretisieren:**
`schema_version`, `event_id`, `run_id`, `agent_id`, UTC-Zeit, `kind`,
`request_id`/`action_id`/`task_id` falls relevant, Ergebnisstatus, Datenschutzklasse,
Referenzen auf getrennt gespeicherte Belege. Keine kompletten Secrets/Prompts im Event.
Zeitpunkte normalisieren; Identität/Idempotenz nicht aus sekundengenauen Zeitstempeln ableiten.

**Zielstruktur, keine Behauptung über vorhandene Dateien:**

- Vorhanden: `web/runtime.py`, `web/decision.py`, `web/observer.py`,
  `memory/gateway.py`, `prompts/resident-system.txt`, `scripts/install-runtime.py`.
- Neu bei Bedarf: `village/config.py`, `village/inference.py`,
  `village/coordination.py`, `village/control.py`, `village/jobs.py`,
  `memory/projections.py`, `tests/integration/`, `docs/evidence/`.
- Standardbibliothek und SQLite bevorzugen. Zusätzliche Bibliotheken nur bei
  begründetem Nutzen, gepinnten Versionen und getesteter Offline-Bereitstellung.

Für Tasks/Inbox bevorzugt ein kleiner lokaler Koordinationsdienst mit eigener UID,
privater SQLite-Datei und Unix-Socket. Client-Identität aus `SO_PEERCRED` und der
installierten UID-Zuordnung ableiten, nicht aus frei angegebenen JSON-Feldern.
Board/JSON bleiben lesbare Projektionen, nicht die manipulierbare Autorität.
Keine neue öffentlich beschreibbare Koordinations-API.

## 3. Reihenfolge und Freigabestufen

| Abschnitt | Pakete | Ergebnis |
|---|---|---|
| A – Betrieb und Schnittstellen | P00–P07 | Sicher pausierbar, reproduzierbar installierbar, authentifizierte Inferenz |
| B – Handlungsfähigkeit | P08–P13 | Verlässliche Inbox, Arbeitsfortschritt, asynchrone Werkzeuge, Ergebnisprüfung |
| C – Wissen und Habitat | P14–P19 | Geschütztes Gedächtnis, echte Projektionen, geprüfte Ressourcenrechte |
| D – Beobachtung und Sicherheit | P20–P22 | Geschützter Kontakt, belastbare Ereignisse, verständliche UI |
| E – Abnahme und Forschung | P23–P26 | Neuinstallation, Recovery und kontrollierter Wiederanlauf nachgewiesen |
| F – bedingte Erweiterungen | P27–P28 | Nachkommen und zweite Kolonie, nur unter ihren Voraussetzungen |
| Abschluss | P29 | Nachweismatrix, Dokumentation und reproduzierbarer Release |

Numerisch vorgehen, soweit die Abhängigkeiten erfüllt sind. Bei fehlender
Host-Freigabe lokale Tests fertigstellen und zu einem unabhängigen lokalen Paket
wechseln; nicht improvisiert auf Produktionssystemen testen.
P27/P28 sind Bestandteil des Gesamtplans, aber kein Vorwand, den stabilen Kern
unfertig zu lassen. Eine nicht bereitgestellte zweite Kolonie ist eine dokumentierte
Voraussetzung, keine angeblich erledigte Funktion.

## 4. Arbeitspakete

### P00 – Verifizierte Baseline und sichere Übergabe

**Abhängigkeiten:** keine. **Dateien:** Audits, Tests, Installer, aktuelle Git-Diffs.

1. Branch, HEAD, Dirty-Status, vorhandene Tests und Unterschiede zum bisherigen
   Audit feststellen. Vorhandene Änderungen als Baseline sichern, nicht bereinigen.
2. Mit freigegebenem Read-only-SSH Host-Units, installierte Dateihashes, Recovery-Hooks,
   Versionen und Rolloutpfade erfassen. Keine `.env` ausgeben; nur erlaubte nichtgeheime
   Metadaten beziehungsweise einen privaten lokalen Hash vergleichen.
3. N02-Proxy-Quellpfad, Container-Mounts und tatsächlichen Eigentümer finden. Der
   Proxy ist bisher **nicht als Repository-Datei lokalisiert**; keinen Pfad erfinden.
4. Konflikte zwischen Host-WebUI und Repo dokumentieren. Sanitisierten Testbestand
   aus repräsentativen Fehlern erstellen, keine privaten Agententexte veröffentlichen.

**Tests:** Repository-/Diff-Prüfung, vorhandene lokale Tests als Ausgangswert und ein
Read-only-SSH-Durchlauf ohne Dienststart. **Abnahme:** Baseline-Bericht mit Datum,
Revision, Testausgabe und unbekannten Punkten; keine veränderte Konfiguration.
**Rollback:** entfällt, read-only; bei versehentlichem Schreibzugriff sofort pausieren,
Änderung isolieren und nur aus einem vorherigen Backup nach Betreiberfreigabe restaurieren.

### P01 – Persistenter Pausezustand

**Abhängigkeiten:** P00. **Dateien:** Bootstrap-Units, `village-resume`, Authority,
`scripts/install-runtime.py`; vorgeschlagen `village/control.py`.

1. Root-kontrollierten persistenten Pausemarker außerhalb agentenschreibbarer Verzeichnisse
   vorsehen; CLI `status`, `pause`, `resume` mit klar unterschiedlicher Wirkung.
2. Alle Startpfade müssen Pause respektieren: systemd-Start, Cron, Recovery,
   Bootstrap, Installer, Rollback und Rechteänderungen durch King. Unit-Condition
   zusätzlich zum CLI-Check; Agenten/King dürfen die Betreiberpause nicht aufheben.
3. Pause vor dem Stoppen atomar setzen. Status muss lokale Dienste und eventuell
   nachlaufende entfernte Inferenz unterscheiden. P07 ergänzt den entfernten Abbruch.

**Tests:** Neustart/Recovery/Authority-Aufruf starten bei Pause keinen Agenten;
idempotentes Pause; Fehler beim Markerzugriff stoppt automatischen Wiederanlauf.
Reboot-Test zuerst isolierte Debian-VM. **Rollback:** Marker niemals automatisch
entfernen; vorherige Units wiederherstellen und gestoppt lassen.

### P02 – Eine Quelle pro Komponente und versionierter Release

**Abhängigkeiten:** P00, P01. **Dateien:** Bootstrap, beide Build-/Install-Skripte,
eingebettete `WEBUI`, `TELEMETRY`, `AUTHORITY` und Recovery-Skripte.

1. Nur benötigte eingebettete Komponenten mechanisch in importierbare Dateien
   auslagern; keine UX-/Logikänderung in diesem Teilpatch. Die neuere Host-WebUI
   erst nach geprüftem Diff als kanonische Quelle übernehmen, keine Blindkopie.
2. Release enthält Quellrevision, Dateihashes, Schema-/Prompt-Versionen und einen
   nichtgeheimen Konfigurations-Fingerprint. Zielhost prüft Manifest vor Installation.
3. Installer erhält Dry-run und explizit „installieren ohne starten“. Backup vor
   Änderung, atomare Dateiersetzung, Rücksicherung der ursprünglichen Rechte.
   Keine automatische Migration rückwärts über ein unbekanntes Datenbankschema.

**Tests:** Build reproduzierbar; Bootstrap und Update installieren dieselben Quellen;
abgebrochener Install rollt sicher zurück; pausierte Dienste bleiben pausiert;
UI-Smoke-Test beweist keinen Versionsrückfall. **Rollback:** letzter geprüfter Release
plus schema-kompatibles Backup; Host-`.env` wird nicht durch ein Repo-Exemplar ersetzt.

### P03 – Einheitliche Konfiguration und Validierung

**Abhängigkeiten:** P02. **Dateien:** `.env.example`, Bootstrap, Installer, neue Config-Hilfe.

1. Bestehende `OLLAMA_AGENT_<n>_*`-Namen rückwärtskompatibel behalten. Pro Agent ergänzen:
   `API_TYPE=ollama|openai`, `API_TOKEN` sowie klar getrennte optionale OpenAI-Parameter.
   Die bisherigen URL-/MODEL-/CTX-Felder bleiben erhalten; kein heimlicher Aliaswechsel.
2. Ein konsistenter Parser für Bootstrap/Update/Prüfung. Erlaubte `.env`-Syntax
   dokumentieren; unbekannte Shell-Ausdrücke nicht mittels `eval` ausführen.
   Nicht unterstützte bestehende Syntax vor Änderungen verständlich ablehnen.
3. IDs, doppelte Namen, URL-Schema, Header-Injection, Zahlen und Kombinationen prüfen.
   `check-config` zeigt nur maskierte Diagnose, niemals Secrets.

**Tests:** leere optionale Tokens, Kommentare/Quotes, unnummerierte Lücken, fehlerhafte
Werte, Default-Verhalten und unveränderte CTX-Werte. Ein Fehler hinterlässt keinerlei
halbgeschriebene Konfiguration. **Rollback:** originale Host-Datei unverändert.

### P04 – Ollama- und OpenAI-kompatibler Inferenzadapter

**Abhängigkeiten:** P03. **Dateien:** Runtime, neue `village/inference.py`, Mock-HTTP-Tests.

1. Schnittstelle zum Bauen der Anfrage und Normalisieren der Antwort isolieren.
   Natives Ollama und OpenAI-kompatible Chat-Completions getrennt behandeln.
2. Bestehende Ollama-Optionen unverändert weitergeben. `num_ctx`/`keep_alive` nicht
   als universelle OpenAI-Parameter senden. Im OpenAI-Modus CTX als lokales
   Eingabebudget deklarieren, nicht als neu reservierten serverseitigen KV-Cache.
3. Tokens per Bearer-Header, nicht URL. Kein Token bei leerer Konfiguration.
   Redirects dürfen Credentials nicht an andere Ziele weiterreichen.
4. Finish-Gründe, Nutzung, leere Antworten, Ablehnungen, HTTP-Fehler und Zeitlimits
   normalisieren. Keine automatische Wiederholung möglicherweise bereits berechneter
   Requests nach unklarem Verbindungsabbruch; insbesondere keine Kosten-Duplikate.
5. Modellspezifische Thinking-/Ausgabelimit-Parameter nur explizit und nach Dokumentation;
   keine vermeintlich universelle Zuordnung, keine Modellsubstitution.

**Tests:** zwei lokale Fake-Server mit verschiedenen Tokens; kein Cross-Agent-Leak;
Ollama-Payload unverändert; OpenAI-Antwort einschließlich `length` korrekt verarbeitet;
401/403/429/5xx, Redirect und kaputte Antworten. Keine echten API-Kosten.
**Rollback:** alter Adapter bleibt für bisherige native Konfiguration lauffähig.

### P05 – Secrets und Authentifizierung über alle Aufrufwege

**Abhängigkeiten:** P04. **Dateien:** Installer, Bootstrap/Pull, Telemetrie,
`ollama-vram-loader.sh`, Runtime, `.gitignore`, README.

1. Je Agent eigene Secret-Datei; Serverzugang nur für zuständige Prozesse.
   Kein vollständiger Token-Satz in einer für `ai-village` lesbaren gemeinsamen Datei.
2. Auth an Inferenz, Ollama-Prozessabfrage, Pull sowie Lade-/Entladehilfe weiterreichen.
   OpenAI-Endpunkte niemals mit `/api/pull` oder `/api/ps` behandeln. Fehlende
   GPU-Prozesstabelle als „nicht verfügbar“, nicht als „Modell entladen“ darstellen.
3. Tokens bei Speicherfehlern, HTTP-Fehlern und Subprozessausgaben maskieren. Nicht
   in Prozessargumenten, Prompt, Manifest oder Browser transportieren; unnötige
   Secret-Variablen aus Werkzeug-Subprozessen entfernen.
4. Dokumentieren: Ein Agent mit beliebigem Code unter derselben UID kann eigene
   Prozess-Secrets gegebenenfalls lesen. Strengere Geheimhaltung verlangt einen
   separaten Inferenzbroker, nicht nur Environment-Filtering.

**Tests:** Secret-Canary taucht nirgends in öffentlichen Ausgaben auf; Dateirechte mit
zwei getrennten Unix-Usern testen; Tokenwechsel/-entfernung überschreibt keine anderen
Credentials. **Rollback:** Secret-Backups privat, keine alten Tokens unbemerkt reaktivieren.

### P06 – Nachvollziehbarer Inferenz-Lebenszyklus

**Abhängigkeiten:** P04, P02. **Dateien:** Runtime, Telemetrie, neuer Job-/Event-Speicher.

1. Request-ID vor Netzaufruf persistent erzeugen. Zustände mindestens
   `queued`, `requesting`, `completed`, `failed`, `cancel_requested`, `cancelled`, `unknown`.
   „Generiert auf GPU“ nur bei entsprechendem Backend-Nachweis behaupten.
2. Aktiven Request und Generation/Version referenzieren. Späte Antworten alter
   Generationen dürfen nach Resume/Configwechsel keine Werkzeuge auslösen.
3. Dauer, Tokenzahlen und Fehlerklasse getrennt speichern. Fehlende Endereignisse
   explizit `unknown` statt dauerhaft „denkt“ anzeigen.

**Tests:** Timeout, Prozessabbruch, späte Antwort und Neustart mit altem Request;
ein Request bekommt keine doppelte erfolgreiche Aktion. **Rollback:** alte Events
weiter lesbar, neue Felder versioniert; keine Umdeutung historischer Zeitreihen.

### P07 – Kontrolliertes Auslaufen und echter Abbruch

**Abhängigkeiten:** P01, P05, P06; tatsächlicher Proxy-Pfad aus P00. **Dateien:**
Control-/Lifecycle-/Inference-Module, Proxy-Adapter, Units und isolierte Mock-Fixtures.

1. „Keine neuen Requests, laufende beenden lassen“ von „abbrechen“ unterscheiden.
   Rollout wartet standardmäßig auf sicheren Übergabepunkt oder bleibt pausiert.
2. Proxy mit lokalem Streaming-/Disconnect-Mock testen. Abbruchweitergabe an Upstream
   implementieren, sofern das tatsächliche Backend sie unterstützt. Kein erfundener
   universeller Cancel-Endpunkt; Netzwerk-Disconnect allein ist kein Erfolgsnachweis.
3. Bei nicht abbrechbarem Backend: `unknown/running remote`, Drain-Frist und explizite
   Betreiberentscheidung. Container-Neustart ausschließlich als benannter Eingriff,
   nie automatisch alle Ollama-Instanzen neu starten oder Modelle entladen.

**Tests:** Fake-Upstream erhält Abbruch oder Drain wartet nachweislich bis Ende;
Late-response-Schutz greift. Host-Abnahme nur nach Freigabe: genau eine isolierte
Lane, Request-Ende über Backend-Nachweis, keine Beeinträchtigung anderer Lanes.
**Rollback:** Proxy-Version wiederherstellen; Simulation pausiert lassen.

### P08 – Transaktionaler Koordinationsspeicher

**Abhängigkeiten:** P02, P06. **Dateien:** `Tasks` in Runtime; neuer lokaler Dienst/SQLite.

1. Aufgaben und Nachrichten nicht länger ausschließlich in gemeinschaftlich
   überschreibbaren JSON-Dateien verwalten. SQLite mit Migrationen, Transaktionen,
   eindeutigen IDs, Versionsprüfung und Zeitlimits für Locks.
2. Bestehende Tasks/Board-Daten idempotent importieren. Beschädigte Daten führen zu
   Quarantäne/Diagnose, nicht zu leerem Default und Überschreiben des Bestands.
3. Schreiber authentifizieren; nur Dienst darf DB/WAL/Backups lesen/schreiben.
   Lesbares Board als kompatible Projektion erhalten, keine Historie löschen.

**Tests:** zwei echte Prozesse claimen gleichzeitig – genau einer gewinnt; Crash
während Migration, Wiederholung des Imports, beschädigte JSON-Datei, voller Datenträger,
unberechtigter User. **Rollback:** versionierte Migration plus geprüfter Restore;
nicht einfach eine alte DB über seitdem entstandene Daten kopieren.

### P09 – Dauerhafte Inbox und explizite Bestätigung

**Abhängigkeiten:** P08. **Dateien:** Snapshot-/Board-Logik, Koordinationsdienst, Parser.

1. Direktnachrichten und organische Nachrichten mit stabilen IDs, Empfänger,
   Thread/Reply-ID, Zustell- und Bestätigungsstatus speichern.
2. Abruf mit Cursor/Pagination; nur tatsächlich in Kontext aufgenommene Nachrichten
   als zugestellt markieren. `ack` ist explizit und nicht mit „kognitiv verstanden“
   gleichzusetzen. Wiederzustellung bis Ack, idempotent verarbeitet.
3. Kontextkürzung, ungültige Antwort oder Crash bestätigen keine unsichtbaren
   Nachrichten. Broadcast fan-out und Aufbewahrung begrenzen, ohne stumm zu verlieren.

**Tests:** mehr Nachrichten als Kontextbudget; Nachricht trifft während Inferenz ein;
Crash vor/nach Ack; doppelte Reply; Neustart; Reihenfolge trotz Zeitzonen.
**Rollback:** unbestätigte Nachrichten exportierbar halten; keine globalen Lesecursor
aus dem höchsten ungesehenen Zeitstempel erzeugen.

### P10 – Dauerhafter Arbeitsfortschritt statt Claim-Schleifen

**Abhängigkeiten:** P08, P09. **Dateien:** Tasks, Runtime-State, gemeinsamer Prompt.

1. Aufgabe erhält Ziel, Kriterium, Owner, Lease, letzten Befund, nächsten konkreten
   Schritt, Blocker, Artefaktreferenzen und Revision. Änderungen sind nachvollziehbar.
2. Erneutes Claim durch denselben Owner wird als idempotente Bestätigung beziehungsweise
   Lease-Erneuerung behandelt, nicht als neuer Fortschritt. Fremde Übernahme nur nach
   gültigem Yield/Lease-Ablauf; alte Worker-Ergebnisse gegen Revision prüfen.
3. Snapshot zeigt eigene offene Arbeit vor allgemeinen Ankündigungen. Blockierte
   Arbeit erlaubt unabhängige Alternativen; King bleibt keine globale Freigabeschlange.
4. Altdaten mit schwachen Kriterien markieren, nicht durch menschlich erfundene
   Erfolgsgeschichten ersetzen. Die Agenten wählen Thema und Lösung weiterhin selbst.

**Tests:** Claim-Wiederholung, Lease-Ablauf, Restart, stale Worker, parallele Updates,
bewusstes Yield und nachvollziehbarer nächster Schritt. **Rollback:** alte Task-Felder
bleiben exportierbar; kein Arbeitsstand verschwindet.

### P10.1 – Dynamische Rollen und plurale Projektteams

**Abhängigkeiten:** P08–P10. **Dateien:** `village/teams.py`, Runtime-Toolvertrag,
Initialprompt, Teamtests und Evidence-Nachweis.

1. Rollen sind Projektmandate und Startlabels, keine unveränderlichen Identitäten.
   Ein Team darf beliebig viele Mitglieder mit derselben Rolle aufnehmen; jedes
   Mitglied behält private Memory, eigene Hypothesen und individuelle Evidenz.
2. Teammandate enthalten Projekt, Ziel, Koordinationsmodus und optionales Ablaufdatum.
   Individuelle Subtasks werden getrennt beansprucht und abgeschlossen; ein Team-Claim
   darf nicht als persönlicher Fortschritt eines anderen Agents gelten.
3. Agents können Teams bilden, beitreten, verlassen und Rollenvarianten angeben.
   Rollenwechsel erfolgen über begründete Vorschläge und eine dokumentierte Mehrheit
   der aktiven Teammitglieder. Es gibt kein globales Eignungsranking und keine
   automatische Belohnung für Konformität.
4. Der Runtime-Snapshot zeigt aktive Teams und Subtasks. Dashboard/Forschung können
   dadurch Einzel-, Mehrfach- und heterogene Teambedingungen getrennt auswerten.

**Tests:** mehrere Agents mit identischer Rolle, getrennte Subtask-Verantwortung,
Mehrheits-Rollenwechsel, abgelehnter Doppel-Claim und Team-Austritt. **Abnahme:**
ein Agent kann ein Team selbst gründen, zwei Peers können dieselbe Rolle übernehmen,
und ein Rollenwechsel bleibt mit Vorschlag, Stimmen und Begründung reproduzierbar.
**Rollback:** Teammandat schließen oder Mitglieder austreten lassen; persönliche
Memory, Artefakte und historische Rollenentscheidungen bleiben erhalten.

### P11 – Fehlerklassifikation, Wiederholungserkennung und Kontextbudget

**Abhängigkeiten:** P06, P09, P10. **Dateien:** Runtime, Parser, Prompt, Tests.

1. Netzwerk-/Auth-Fehler, ungültige Aktionen, Ausgabelimit, Werkzeugfehler und bewusste
   Ruhe unterscheiden. Retry nur passend zur Ursache, mit Backoff und sichtbarem Zustand.
2. Signatur mit normalisierter Aktion, Ziel und Ergebnis-Fingerprint; nicht nur rohen
   Befehlsstring vergleichen. Sichere Regeln zuerst, kein zusätzliches Judge-LLM nötig.
   Fortschritts-Polling mit neuem Ergebnis nicht als sinnlose Wiederholung sperren.
3. Recovery-Kontext enthält konkrete vorherige Aktion, Fehler und ungelöste Hypothese;
   weniger wiederholte Peer-Behauptungen. Kein automatischer Shellbefehl als „Divergenz“.
4. Eingabe-/Ausgaberäume budgetieren; Tokenmessungen nutzen, Schätzungen kennzeichnen.
   KV-Auslastung nicht aus VRAM oder Kontextlänge erfinden. Kürzung darf Inbox-Acks
   nicht vorziehen und eigene Ziele/letzte Ergebnisse nicht lautlos entfernen.

**Tests:** `pwd`-Schleife, paraphrasierte Wiederholung, legitimes Polling, Auth-Fehler,
15-Minuten-Backoff nach Neustart, 8K-Kontext und unvollständige Aktionsblöcke.
**Abnahme:** weniger Loops ist zunächst nur eine Live-Hypothese; lokale Tests beweisen
nur Controller-Verhalten. **Rollback:** Klassifikations-/Backoffregeln versioniert
zur vorherigen Regelmenge zurückschalten, Ereignisse und Fehlerhistorie behalten und
keine Requests automatisch löschen oder nachträglich umetikettieren.

### P12 – Ereignisorientierung und langlebige Werkzeugjobs

**Abhängigkeiten:** P06–P11. **Dateien:** Runtime, neuer Job-Worker, lokale Koordination.

1. Kein globaler Rundentakt. Inbox-Ereignisse wecken einen bereiten Agenten, ergänzt
   um Idle-Timer mit Jitter/Backoff. Höchstens eine Inferenz je Agent/Lane, außer
   explizit konfigurierter und getesteter Parallelität.
2. Lange Shell-/Buildjobs erhalten persistente IDs und Status. Ergebnis sammeln,
   ohne Nachrichten dauerhaft unzugänglich zu machen. Zunächst maximal ein mutierender
   Werkzeugjob pro Agent; reine Inferenz/Kommunikation kann unabhängig weiterlaufen.
3. Prozessgruppen beziehungsweise dedizierte cgroups, Timeout, begrenzte Ausgaben und
   nachvollziehbare Abbrüche. Nach Crash unbekannte nicht-idempotente Aktionen nicht
   blind erneut ausführen: Zustand untersuchen und als unklar melden.

**Tests:** Agent A baut lange, B arbeitet weiter; neue Nachricht wird angeboten;
Crash in Start/Ergebnis-Übergabe; keine doppelten Deployments; pausiert startet nichts.
**Rollback:** Worker-Jobs gezielt abwickeln, nicht pauschal alle Container beenden.

### P13 – Reproduzierbare Artefakte und unabhängige Verifikation

**Abhängigkeiten:** P10, P12. **Dateien:** Koordination, neuer Verifier, Auswertung.

1. Artefaktmanifest mit Owner, Dateihash/Revision, Testbeschreibung, benötigten
   Ressourcen und Provenienz. „Geplant“, „erstellt“, „behauptet erfolgreich“,
   „reproduziert“ und „von anderem Agent genutzt“ unterscheiden.
2. Verifikation getrennt vom Autor protokollieren. Tests aus Agentenbeiträgen sind
   untrusted Code: unprivilegiert, zeit-/ressourcenbegrenzt, ohne Secrets ausführen.
3. Peer darf Ergebnisse prüfen; passive Forschungsbewertung bleibt außerhalb der
   Agentenprompts. Menschliche/LLM-Urteile als solche kennzeichnen, nicht als Fakten.

**Tests:** `true` ohne Artefakt, Fehler durch `|| echo` verdeckt, erfundenes Testergebnis,
geänderte Datei nach Prüfung, fremder Owner und gültiges reproduzierbares Minibeispiel.
**Rollback:** Belege bleiben erhalten, keine nachträgliche Aufwertung alter Erfolge.

### P14 – Memory-Sicherheit, Konsistenz und Betriebsfähigkeit

**Abhängigkeiten:** P03, P05, P08. **Dateien:** Gateway, CLI, Units, Memory-Tests.

1. Fail-closed bei fehlenden/defekten Auth-Dateien; zulässigen anonymen Healthcheck
   ausdrücklich trennen. Nutzeridentität aus Credential ableiten, nicht Payload vertrauen.
2. API-Filter allein genügen nicht: DB, WAL, Backups und Unterverzeichnisse dürfen
   nicht durch andere Agent-UIDs direkt lesbar sein. UID von WebUI und Memory-Dienst
   trennen, falls sonst ein öffentlicher UI-Prozess alle privaten Erinnerungen lesen kann.
3. Vollständige Eingabevalidierung, atomare Quoten, stabile IDs/Idempotency-Key,
   Update/Delete/Expiration mit Ownership und konsistenter Provenienz vorsehen.
4. Private/shared-Semantik, kaputte DB, voller Datenträger und Restore behandeln.

**Tests:** anderer User über HTTP und direktes Dateisystem, falscher Owner, kaputte
Limits/UTF-8/Confidence, gleichzeitige Writes, doppelte Requests, alte Tokens und
Quota-Rennen. **Rollback:** überprüftes privates Backup; keine neue anonyme Freigabe.

### P15 – Dauerhafte Projektionswarteschlange

**Abhängigkeiten:** P14. **Dateien:** Gateway, neue Memory-Projektionsmodule.

1. SQLite bleibt Quelle. Änderung und Outbox-Eintrag in derselben Transaktion speichern.
   Projektion durch getrennten Worker, nicht synchron im Agentenrequest.
2. Zustand je Backend: Revision, letzte erfolgreiche Projektion, Retry, Fehler und
   Rückstand. Wiederholbare Upserts und Tombstones für Löschungen/Scopewechsel.
3. Wiederaufbau, Offline-Betrieb und Fehlerisolation; Ausfall einer DB blockiert weder
   Speichern noch die andere Projektion. Keine unendlichen schnellen Retry-Schleifen.

**Tests:** Crash zwischen Commit/Projektion, doppelte Zustellung, Backend offline,
Scopewechsel privat→shared→privat, Delete und kompletter Index-Neuaufbau.
**Rollback:** Worker abschalten, SQLite bleibt verwendbar; nicht einfach Primärdaten löschen.

### P16 – Echte ChromaDB-Anbindung

**Abhängigkeiten:** P15; freigegebene gepinnte Chroma-Version und lokales Embedding-Modell.
**Dateien:** Memory-Adapter, Compose/Images, Offline-Abhängigkeiten.

1. Bestehende Version/API prüfen; nicht aufgrund wechselnder `latest`-Images entwickeln.
   Embedding-Modell inklusive Lizenz, Hash, Dimension und Laufzeit bereitstellen.
   Keine automatische Modell-/Telemetrie-Verbindung ins Internet.
2. Private/shared-Namensräume durchsetzen. Direkten ungeschützten Backend-Zugang
   anderer Host-User verhindern; Loopback allein ist keine User-Isolation.
3. Semantische Suche mit gespeicherter Modellrevision; lexikalischen Fallback sichtbar
   beibehalten. Treffer vor Ausgabe erneut gegen aktuelle Ownership/Scope prüfen.

**Tests:** echtes gepinntes Backend im Testnetz; Upsert/Delete/Restore, fremde UID,
Embedding-Fehler, keine externen Downloads und Suche in einem festen Referenzdatensatz.
**Rollback:** Adapter deaktivieren; Primärgedächtnis unverändert nutzbar.

### P17 – Echte Neo4j-Anbindung

**Abhängigkeiten:** P15; freigegebene gepinnte Neo4j-Version. **Dateien:** Graph-Adapter/Compose.

1. Kleines Schema: Agent, Memory, Task, Artifact, dokumentierte Beziehungen mit
   Quellenreferenz und Status `observed/claimed/inferred`; keine erfundenen Faktenkanten.
2. Parametrisierte Queries, minimale technische Credentials, Namespaces/ACLs wie P16.
   Keine unkontrollierten Cypher-Aufrufe von Agenten mit globalen Schreibrechten.
3. Provenienzabfragen implementieren; Produkt-/Editionsabhängigkeiten prüfen. Keine
   Enterprise-Mandantenfähigkeit versprechen, wenn nur Community verfügbar ist.

**Tests:** echte Graph-Projektion, Wiederholung, Scopewechsel, Löschung, Rebuild,
fehlende DB und Fremdzugriff. **Rollback:** Graph-Worker aus; SQLite bleibt maßgeblich.

### P18 – Fähigkeiten und Rechtevergabe durch King

**Abhängigkeiten:** P01, P08. **Dateien:** Authority, Unix-Gruppen/Units, Capability-Manifest.

1. Tatsächliche Fähigkeiten getrennt von sozialen Rollen führen. Benutzerwunsch:
   alle Agenten sollen rootless Container nutzen können; `resident` nicht versehentlich
   durch die alte Rollenlogik ausschließen. Kein Docker-Socket oder pauschales sudo.
2. Rechteanfragen mit ID, Umfang, Grund, Entscheidung und gegebenenfalls Ablauf.
   Agent erhält verlässliche Bewilligung/Ablehnung, nicht nur eine Board-Ankündigung.
3. GPU-Entzug gegen reale Geräterechtspfade, `video`/`render`, ACLs und laufende Prozesse
   prüfen. Gruppenänderung allein widerruft bestehende offene Handles nicht. Notwendigen
   Workload-Stopp ausdrücklich ausweisen; keine Fremdprozesse abschießen.
4. Authority muss Pause respektieren und Teilfehler berichten; kein `ok` nach
   gescheitertem Rechteentzug/Neustart.

**Tests:** unberechtigter Aufrufer, gefälschte ID, Grant/Revoke, Teilfehler, Pause,
offenes GPU-Handle und zurückgenommene Rechte nach neuem Login/Prozessstart.
**Rollback:** vorherige benannte Rechte wiederherstellen, kein generelles chmod 777.

### P19 – Container, Datenträger und M10 experimentell benutzbar machen

**Abhängigkeiten:** P18, P02. **Dateien:** Bootstrap, GPU-/Podman-Hilfen, Integrationstests.

1. Aktuellen Host inventarisieren: CPU/RAM, Mounts, freie Größen, User-/SubUID-Mappings,
   Runtime-Verzeichnis, CDI, Treiber und Compose-Provider. Alte Defekte erneut messen.
2. Pro Agent und in einer ausgewiesenen gemeinsamen Testablage rootless Build/Run,
   Persistenz und Cleanup prüfen. Rechte nur an ausdrücklich freigegebenen Verzeichnissen;
   gemeinsamer und privater Storage müssen koexistieren. Bestehende Daten erhalten.
3. CDI/GPU-Zugang mit gepinntem, bereits verfügbarem kompatiblem Testimage testen.
   `nvidia-smi` allein beweist keine funktionierende GPU-Berechnung im Container.
4. Messbare Capability-Beschreibung für Agenten: gültige Pfade, verfügbare CLIs
   einschließlich Wikipedia/Signals, Anfrageweg zu King. Keine neue Zwangsmission.

**Tests:** jeder Benutzer; kleine Datei und kleiner Container; einzelne GPU mit
bekanntem Rechenergebnis; kein externes Pull ohne Freigabe; sauberer Stop/Restart.
**Rollback:** nur erzeugte Testressourcen entfernen, ursprüngliche ACLs sichern.

### P20 – Öffentliche Signals-/Contact-Sicherheit

**Abhängigkeiten:** P00, P02, P05. **Dateien:** aktuelle kanonische WebUI, Proxy-Konfiguration,
Frontend-Formulare, neue HTTP-/Browsertests.

1. Login vor Eingabe; Nachricht nach Send genau einmal in korrekter Historie.
   Fail-closed ohne Credentials, sichere Session-ID, Ablauf/Logout und Sessionrotation.
2. CSRF-Schutz einschließlich Login, Requestgrößenlimit, bounded Rate-Limit, sicherer
   Cookie-Modus für HTTPS, keine offenen Redirects (`//fremd` ist nicht intern).
3. Forwarded-Header nur von konfigurierten vertrauenswürdigen Proxies akzeptieren.
   Öffentliche Lesbarkeit nicht mit Berechtigung auf private Inbox/Memory verwechseln.
4. Markdown/Links/Dateipfade gegen XSS und Traversal testen. Untrusted Signale bleiben
   Daten; keine direkte Werkzeugausführung aus HTTP-Inhalten.

**Tests:** anonym, falscher Login, CSRF, Replay/Doppelclick, Proxy-Spoofing, Redirect,
Sessionablauf und Login→Compose→Send→History im Browser. Nur synthetische Nachrichten
im Test-Village, kein Schreiben in die reale Simulation.
**Rollback:** gesicherte aktuelle UI; bei Auth-Fehler Schreibkanal schließen, nicht öffnen.

### P21 – Vollständige, getrennte und bezahlbare Beobachtung

**Abhängigkeiten:** P06, P08, P13, P15. **Dateien:** Observer, Collector, Ereignisspeicher.

1. Wegwerfbare Telemetrie und beweisrelevante Ereignisse unterscheiden. Nicht-blockierendes
   Locking darf keine Inferenz-/Aktions-Enden lautlos verlieren; private Spools mit
   IDs und idempotentem Import, Rückstand/Zähler für tatsächlich verlorene Messungen.
2. Rotation, Retention und Langzeitaggregate mit offen gelegtem Nenner; vollständige
   Historie nicht mit den letzten 500 Zeilen verwechseln. Keine privaten Inhalte publizieren.
3. Collector-/Auswertedaten von Agentenzugriff trennen. Runtime-Eigenfeedback bleibt
   erlaubt; Forschungsbewertungen werden nicht zurück in Agentenkontext eingespeist.
4. Externen Export optional anbieten, lokal begrenzen. CPU/I/O/RAM-Overhead messen;
   „keine Verhaltensrückkopplung“ statt „physikalisch ohne Einfluss“ dokumentieren.

**Tests:** parallele Writer, Rotation/Restart, Collector offline, doppelte Events,
volle Spool, Schemawechsel und fremde UID. **Rollback:** Archiv erhalten; keine
rückwirkende Löschung missliebiger Fehler.

### P22 – Dashboard: Fortschritt, Wissen und Unsicherheit richtig zeigen

**Abhängigkeiten:** P09–P10, P13, P16–P17, P20–P21. **Dateien:** bestehende `web/observatory.*`
und kanonische WebUI; kein Design-Neustart.

1. Zustände Queue/Inferenz/Tool/Backoff/Pause/unbekannt, Inbox und Aufgabenfortschritt
   sichtbar machen. Infrastruktur-Details bleiben funktionsfähig.
2. Memory-Metriken trennen: Primäreinträge, Vektoren, Graphobjekte, Indexrückstand,
   Suchvorgänge und belegte Wiederverwendung. Wachstum nicht als Intelligenz etikettieren.
3. Prozessowner von Initiator unterscheiden; Container-/GPU-Zuordnung mit Evidenzgrad
   oder „unbekannt“. Keine erfundenen Microservice-Verbindungen.
4. Dark/Light/Color, mobile Ansicht, Fokus/Keyboard, Pagination/Filter, Markdown und
   Anker testen. Keine externen Fonts, CDN, Analytics oder Browser-API-Secrets.

**Tests:** wechselnde Agentenzahl statt hartcodierter neun, leere/alte Daten,
große Ereignismenge, verschiedene Provider, Tastatur und null externe Browserrequests.
**Rollback:** vorheriges UI-Release; neue Serverfelder rückwärtskompatibel.

### P23 – Reproduzierbare Integrations- und Neuinstallationstests

**Abhängigkeiten:** P01–P22 lokal geprüft. **Dateien:** Tests, Bootstrap, Test-Dokumentation.

1. Offline Mock-Suite als Standard; aktuell vorhandene Regressionstests erhalten.
   Testserver dürfen lokale Ports nutzen, aber keine produktiven Modellendpunkte.
2. Bare-Debian-13-VM mit systemd: Neuinstallation, Wiederholung, mehrere Agentenzahlen,
   reine Ollama-, OpenAI-kompatible und gemischte Testkonfiguration. Keine Produktions-`.env`.
3. Migration aus altem Stand, Crash/ENOSPC, Rechteprüfung mit mehreren Unix-Usern,
   Rollback und Secret-Scan. Fehler müssen reproduzierbare Artefakte hinterlassen.
4. AirGap-Abnahme mit vorbereiteten Debian-/Image-/Git-Ressourcen, gesperrten
   übrigen Zielen, gepinnten Abhängigkeiten und ohne implizite Modell-Downloads.

**Tests:** vollständiges Testprotokoll je Umgebung; Tests laufen in einer isolierten
Debian-13-VM und verwenden ausschließlich synthetische Credentials. Container ohne
systemd reicht nicht als Reboot-Test. **Abnahme:** Protokoll je Umgebung.
**Rollback:** Test-VM-Snapshot, niemals Produktionsdaten löschen.

### P24 – Updates, Reboot, Backup und Wiederherstellung

**Abhängigkeiten:** P01, P07, P18, P23. **Dateien:** Update-/Resume-Skripte, Units, Runbook.

1. Doppelte Cron-/systemd-Starts bleiben idempotent. Pause überlebt Reboot;
   freigegebener Running-Zustand stellt genau eine Runtime pro Agent wieder her.
2. Updates nur im vereinbarten Wartungsfenster, laufende Arbeit drainen, danach
   Versions-/API-Kompatibilität prüfen. Bestehende AUTO_UPDATE/AUTO_REBOOT-Werte erhalten.
3. Konsistentes Backup von DBs, Artefaktmanifesten und privaten Credentials;
   Restore in separater VM üben. Projektionen aus Quelle wiederherstellen können.

**Tests:** Pause-Reboot, Running-Reboot mit Mock, Netzwerk/Provider beim Boot offline,
Crash im Update und Restore auf anderem Host. **Host-Gate:** echter Reboot nur mit Freigabe.
**Rollback:** Wartungsfenster abbrechen, Pause-Marker setzen und den vorherigen
geprüften Release samt Backup in einer separaten Restore-Umgebung verwenden; kein
erzwungener Rollback eines unbekannten laufenden Requests.

### P25 – Forschungsprotokoll und echte Fähigkeitsprofile

**Abhängigkeiten:** P13, P21. **Dateien:** neues Forschungsprotokoll, Auswertung, Audit.

1. Vor Live-Vergleich Hypothesen und Messgrößen festlegen: reproduzierte Artefakte,
   Wiederverwendung, Fehlererholung, unbeantwortete Nachrichten, Kosten/Tokens und Zeit.
   Fehler-/Erfolgsquote je Aufgabentyp mit Nenner und Unsicherheit, kein pauschaler IQ.
2. Modell-/Prompt-/Runtime-/Datensatzversionen, Konfiguration und Interventionen erfassen.
   Isolierte Agenten, heterogene Gruppe und optionaler King als getrennte Bedingungen;
   keine heimlichen A/B-Wechsel in der laufenden Kolonie.
3. Kleinen reproduzierbaren Fähigkeits-Testbestand mit bekannten Antworten getrennt
   von freier Entwicklung führen. Kein Training auf Auswertungsergebnissen ohne Kennzeichnung.
4. Vergleich bei ähnlichem Token-/Zeitbudget, mehrere Wiederholungen nach Freigabe.
   Ein Judge-LLM ist optional und gegen menschliche/automatische Referenzen zu prüfen.

**Abnahme:** Protokoll vor Experiment datiert; negative Ergebnisse sind zulässig;
Skill-Wachstum ist von Modellgewichtstraining und sprachlichem Selbstbericht getrennt.
**Tests:** Protokollschema, Nenner-/Unsicherheitsberechnung, Wiederholbarkeit mit
synthetischen Aufgaben und Nachweis, dass Modell-/Promptwechsel als Intervention
markiert werden.
**Rollback:** neues Experimentsegment, nicht historische Bewertungen überschreiben.

### P26 – Gestufter Rollout und Live-Verhaltensabnahme

**Abhängigkeiten:** P01–P25 geprüft, Host-Konfiguration final, ausdrückliche Startfreigabe.
**Dateien:** Release-Manifest, Rollout-/Pause-CLI, Agent-Units, Canary-Konfiguration,
Beobachtungs- und Evidence-Runbook.

1. Zunächst ausrollen ohne Start. Hash-/Schema-/Secret-/UI-Checks, keine Modellkalibrierung.
2. Ein freigegebener Canary-Agent; danach kleine Gruppe; erst dann alle Agenten.
   Keine parallelen offenen Anfragen auf derselben Lane durch Deployment erzeugen.
3. Integrations-Szenario getrennt von freier Forschung: Nachricht empfangen/bestätigen,
   Aufgabe wählen, kleines Artefakt erstellen, echten Fehler korrigieren, Erinnerung
   speichern/abrufen, Peer-Reproduktion, Neustart und Weiterarbeit.
4. Danach mindestens ein vorab vereinbartes freies Beobachtungsfenster, z.B. 24 Stunden.
   Dessen tatsächliche Dauer und Modellgeschwindigkeit berichten, nicht simulieren.

**Tests:** Mock-End-to-end-Szenario plus ein separat freigegebenes Canary-Szenario;
alle Schritte erhalten Request-, Task-, Artefakt- und Memory-IDs. **Abnahme:**
End-to-end-Belege statt nur `active`. Fortschrittsszenario muss reale
Dateien/Tests und verwendete Memory-Treffer zeigen. Scheitert freie Entwicklung,
Status `FAILED_ACCEPTANCE` mit Befunden; keine weiteren ungeplanten Promptänderungen
innerhalb desselben Experiments. Technisch bestandene Teile getrennt ausweisen.
**Rollback:** pausieren, betroffene Revision zurücknehmen, historische Daten erhalten.

### P27 – Reproduzierbare Nachkommen-/Modellpipeline (bedingt)

**Abhängigkeiten:** P13, P19, P24–P26; Betreiberbudget und freies GPU-Zeitfenster.
**Dateien:** Lineage-Schema, Worker-/Dataset-Manifest, Evaluationsskripte und
Lifecycle-/Quota-Konfiguration; neue Dateien erst nach Gate anlegen.

1. Zuerst kleine Skills/Prompts/Programme als Lineage-Artefakte mit Eltern, Version,
   Evaluation und Rücknahme abbilden; nicht als biologische Reproduktion ausgeben.
2. Training/LoRA/kleines Nicht-LLM-Modell als gesonderter Worker mit Messung,
   Datensatz-/Lizenznachweis und reproduzierbarem Test. Klein anfangen; nicht voraussetzen,
   dass 7B inklusive KV-Cache/Trainingszustand in 8 GB passt.
3. Aufnahme eines Kind-Agenten ist ein geprüfter Lifecycle mit begrenzter Zahl,
   Endpoint-/GPU-Zuteilung, eigenen Rechten und beobachtbarem Ende. Keine unbegrenzte
   Selbstvermehrung oder Änderung der initialen Root-Konfiguration durch Agenten.

**Tests:** synthetischer Kleinst-Datensatz, sauberer Abbruch, Ressourcenobergrenzen,
Lineage/Retention, Replay der Evaluation und vollständiger Rückbau des Testkindes.
**Gate:** kein teures Training ohne Freigabe; bis dahin `DEFERRED_CONDITION`.
**Rollback:** Child-Worker stoppen, seine Endpoints und Capabilities entziehen und
Lineage/Evaluation als Beleg behalten; Eltern-Agenten, Host-`.env` und Basisprompt
nicht rückwirkend verändern.

### P28 – Interkolonie-UDP und Besuchsangebote (bedingt)

**Abhängigkeiten:** P09, P18, P20, P24–P26; zweite Kolonie und erlaubter UDP-Port.
**Dateien:** isolierter Transportadapter, Protokoll-/Peer-Konfiguration,
Replay-/Rate-Limit-Store und Offline-Netztest-Fixtures.

1. Transportadapter mit festem konfiguriertem Port, Größen-/Ratenlimits, Peer-Allowlist,
   beobachtbarer Absenderidentität und Replay-/Duplikatbehandlung. Zunächst zwei lokale
   Test-Netzbereiche; nichts öffentlich exponieren oder Firewallregeln automatisch ändern.
2. Nutzprotokoll bleibt Experiment der Agenten. Die Infrastruktur begrenzt Transport
   und Autorität, diktiert aber keine Gesellschaft oder Verhandlungssprache.
3. „Besuch“ zunächst Nachricht/Artefaktangebot; keine automatische Remote-Shell,
   Useranlage oder Übernahme fremder Privilegien. Import ist explizit und überprüfbar.

**Tests:** Paketverlust, Verdopplung, Umordnung, Spoofing, übergroße Pakete und
unbekannter Peer. **Rollback:** Adapter deaktivieren; beide Kolonien bleiben eigenständig.
Ohne zweite Kolonie Host-Abnahme ausdrücklich offen lassen.

### P29 – Abschluss, Release und vollständige Nachweismatrix

**Abhängigkeiten:** Kern P00–P26; P27/P28 entweder abgenommen oder mit Bedingung offen.
**Dateien:** README, `.env.example`, Runbooks, Statusliste, Release-Manifest.

1. Jede identifizierte Lücke auf Paket, Test und verbleibende Einschränkung abbilden.
   Veraltete Audit-Aussagen mit Datum berichtigen, nicht als aktuelle Fakten weiterführen.
2. Dokumentation aus aktuellen nichtgeheimen Einstellungen ableiten. Betriebsbefehle
   für Pause/Resume, API-Zugang, Tokenrotation, Backup, Restore und Drift-Prüfung zeigen.
3. Vom Betreiber freigegebene Änderungen gezielt committen; keine Dirty-Baseline
   oder Secrets versehentlich einschließen. Push nur bei ausdrücklichem Auftrag.
4. „Kern technisch geschlossen“, „Live-Verhalten abgenommen“ und „Erweiterungen
   abgeschlossen“ separat berichten. Keine pauschale Aussage „alles fertig“ mit offenen Gates.

**Tests:** vollständiger Release-Check in sauberer Testumgebung: Install, Pause,
Restore, Secret-Scan, Hash-/Schema-Abgleich sowie alle verpflichtenden Regressionstests.
**Abnahme:** fremder Ausführender kann aus README + Test-.env neu installieren,
pausieren und wiederherstellen; Release entspricht installierten Hashes. Jeder
offene Punkt hat einen Owner beziehungsweise eine konkret fehlende Voraussetzung.
**Rollback:** Release nicht veröffentlichen beziehungsweise auf den letzten geprüften
Release zurückgehen; Status- und Evidence-Dateien nicht löschen, sondern den
fehlgeschlagenen Kandidaten samt Ursache markieren.

## 5. Test- und Übergabeformat

### Vollständigkeitsmatrix

| Identifizierte Lücke / früherer Projektwunsch | Zuständige Pakete |
|---|---|
| Reboot startet pausierte Simulation erneut | P01, P24 |
| Fehlende individuelle API-Tokens / unterschiedliche Provider | P03–P05 |
| Nachlaufende Inferenz, lange Generierung, unklare GPU-Aktivität | P06–P07, P21–P22 |
| Host/Repo/Runtime auseinander, Dashboard zurückgerollt | P00, P02, P23, P29 |
| Fehlende oder verlorene Direktnachrichten, organische Aufträge wiederholt | P08–P09 |
| Claim-/Ankündigungsschleifen, keine nächste Handlung | P10–P12 |
| Starre Rollen, keine Mehrfachbesetzung und keine gesellschaftliche Rollenrotation | P10.1, P25, P26 |
| Ungültige Ausgaben, zu großer Kontext, vorgetäuschte Ressourcennot | P04, P11, P19 |
| Lange Werkzeuge blockieren Reaktionsfähigkeit | P12 |
| Exitcode oder Behauptung als fälschlicher Erfolgsnachweis | P13, P21, P25 |
| Memory-Credentials, fehlende Zugriffstrennung, Datenverlust | P05, P14 |
| Chroma-/Neo4j-Container ohne tatsächliche Gedächtnisnutzung | P15–P17, P26 |
| King-Rechtevergabe/-entzug nicht zuverlässig | P18 |
| Containerrechte, /mnt, lokale M10/CDI nicht für alle nachgewiesen | P19 |
| Öffentlicher Kontakt: Login, Historie, CSRF, Sessions, Markdown | P20, P22 |
| Unvollständige Events, begrenzte Historie, Beobachter beeinflusst Welt | P21, P25 |
| Infrastrukturdetails, Microservice-/GPU-Zuordnung, Themes, Eventflut | P21–P22 |
| Neuinstallation, AirGap, Dependencies, Reboot und Umzug ungeprüft | P23–P24 |
| Unbelegte Intelligenzkurve / individuelle Stärken und Schwächen | P13, P25 |
| Keine kontrollierte Prüfung von Schwarmleistung / Forschungslücke | P25–P26 |
| Fortpflanzung, Skills, eigene kleine Modelle | P27 |
| Zweite Kolonie, UDP-Protokoll, Besuche | P28 |

TRON-/Speziesanalogien und Selbstberichte bleiben gestalterischer Kontext, kein
technischer Abnahmenachweis. Einzigartigkeit und künstliches Bewusstsein werden
nicht als durch eine Implementierung „geschlossene GAP“ versprochen.

Bereits vorhandene lokale Prüfungen (im Repository-Root):

```bash
python3 -m unittest discover -s tests -v
bash -n bootstrap-ai-village.sh
bash -n scripts/install-observatory.sh
bash -n ollama-vram-loader.sh
node --check web/observatory.js
git diff --check
```

HTTP-Tests brauchen lokale Socket-Berechtigung. Sandbox-Verweigerung ist nicht
gleich Testfehler der Anwendung: benötigte Freigabe anfordern, nicht Tests überspringen
und „grün“ melden. Browser-Tests aktuell nicht blind starten: ihr Default zeigt auf
den produktiven Host und nimmt neun Agenten an. In P22/P23 auf Test-URL/Fixtures umstellen.

Pro Paket diesen Bericht in `docs/evidence/Pxx.md` führen (Dateien erst bei echter Arbeit anlegen):

```text
Paket / Unterpaket:
Baseline-Revision und relevante bereits vorhandene Änderungen:
Diagnose mit Datei/Funktion oder datierter Host-Messung:
Implementierte Änderungen:
Ausgeführte Tests, Befehle, Exitcodes und kurze Ergebnisse:
Nicht ausgeführte Tests und Grund:
Migration / Rollback / Auswirkungen auf andere Pakete:
Host-Freigabe, betroffene Ziele und Deployment-Nachweis (falls vorhanden):
Simulation weiterhin pausiert? / .env unverändert?:
Status: LOCAL_VERIFIED oder HOST_VERIFIED oder konkreter Blocker
Nächster erlaubter Schritt:
```

Es ist nicht sinnvoll, alle Pakete einem kleinen Modell in einem einzigen Auftrag
zur kompletten autonomen Erledigung zu geben. Das Modell liest Startregeln, Status
und genau das nächste Ticket, implementiert es und übergibt überprüfbare Ergebnisse.
Sicherheits-/Migrations-/Abbruchänderungen erhalten eine gesonderte Review, bevor sie
Produktivsysteme verändern. Das spart mehr als ein großer ungeprüfter Gesamtpatch.

## 6. Quellen für das API-Paket

Die offizielle OpenAI-Dokumentation beschreibt serverseitige Bearer-Authentifizierung
und die Geheimhaltung von API-Schlüsseln: [API Overview](https://developers.openai.com/api/reference/overview).
Der OpenAI-kompatible Modus ist hier ausdrücklich als Chat-Completions-Adapter geplant:
[Chat API](https://developers.openai.com/api/reference/resources/chat).
Bei Umsetzung aktuelle Parameter und Modellunterstützung erneut prüfen. Dieser Plan
führt keine Migration auf ein bestimmtes Modell, SDK oder die Responses API durch.
