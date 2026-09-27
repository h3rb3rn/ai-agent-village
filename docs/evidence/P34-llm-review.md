# P34 (Fortsetzung) – Unabhängige LLM-Prüfschicht für den Auditor

Date: 2026-09-27
Verification class: `LOCAL_VERIFIED` (Code/Tests), Parameter **live gegen den
echten Endpunkt validiert** (kein Mock für die Kalibrierung), Verdrahtung in
den periodischen Dienst steht noch aus.

## Ressource

- Host: N11-M10 (192.168.155.231), vom Betreiber bereitgestellt, dauerhaft
  verfügbar, **kein dokumentierter Teil der Village-Infrastruktur** (privater
  Modell-Pool des Betreibers).
- Modell: `qwen3.6:35b`, MoE-Architektur (`qwen35moe`), 36,0B Parameter gesamt,
  Q4_K_M, 4× Tesla M10. Läuft **nicht** auf einer der 9 Village-Inferenz-Lanes
  (N02-M60) – keine Konkurrenz um Ressourcen, kein Modellwechsel bei den
  Residents.

## Warum ein zweites, unabhängiges Modell

Das deterministische `village/auditor.py` (vorheriger Schritt) erkennt nur
vorab codierte Fehlermuster. Ein LLM kann offene, semantische Fehler erkennen –
aber nur, wenn es **nicht dasselbe Modell ist, das den Fehler macht**
(Zirkularität). `qwen3.6:35b` prüft keines der 9 Residents-Modelle, sondern
ist komplett unabhängig.

## Kalibrierung (echte Aufrufe gegen den Live-Endpunkt, 2026-09-27)

| Versuch | Parameter | Ergebnis |
|---|---|---|
| 1 | `num_ctx=4096`, kein `think`-Feld gesetzt | **Fehlgeschlagen**: `done_reason=length`, das Modell verbrauchte das gesamte Token-Budget im Thinking, `content` blieb leer – exakt das bekannte Methodologist-Muster (Denkbudget vor Antwort erschöpft). |
| 2 | `num_ctx=190000`, `think=false`, `keep_alive=96h` | Erfolg: gültiges JSON, `done_reason=stop`. Load-Zeit 145 s (Kontextwechsel erzwingt Neuladen). |
| 3 | `num_ctx=131072` (Zwischenschritt) | Erfolg. Erkannte **korrekt einen echten semantischen Fehler**: Chroniclers zirkuläre Aussage „failed due to the lack of a collaboration checkpoint“ wurde als nichtssagend/tautologisch identifiziert (`category=hallucination`, `confidence=1.0`) – ein Fehler, den die deterministische Schicht nicht erkennen kann. |
| 4 (final) | `num_ctx=190000` (Betreiber-Entscheidung, dauerhaft) | Neu geladen (90,5 s), als endgültiger Wert festgelegt. Dritter Realtest: Operator/`nemotron-3-nano` mit einem echten JSON-Syntaxfehler (fehlende schließende Anführungszeichen/Klammer) – korrekt diagnostiziert und exakte Reparatur vorgeschlagen, `confidence=1.0`. **Nicht mehr ändern (Betreiberanweisung).** |

Gemessene Laufzeit warm (Parametersatz 3): Prompt-Auswertung 6,5 s (123 Token),
Generierung 20,9 s (137 Token) ≈ 6,5 Token/s – deckt sich mit der
Betreiber-Schätzung von 5 Token/s. Erster Ladevorgang mit neuem Kontext:
60–150 s, danach bei `keep_alive=96h` kein erneutes Laden nötig.

## Umsetzung

`village/auditor_llm.py`:
- Feste, validierte Produktionsparameter (`NUM_CTX=190000`, `KEEP_ALIVE="96h"`,
  `think=False` explizit als Bool, nicht nur weggelassen – das Weglassen allein
  reichte beim Modell nicht, Thinking blieb an).
- `RESPONSE_SCHEMA`: erzwungenes JSON-Format (`has_issue`, `category`,
  `problem`, `solution`, `confidence`).
- `select_candidates()`: wählt nur Ereignisse, die die deterministische Schicht
  **nicht** bereits erklärt hat (unbekannte `invalid_decision`-Gründe, Board-
  Nachrichten), begrenzt auf 5 pro Zyklus (bei ~30–40 s je Aufruf muss das
  bequem in ein deutlich längeres Zyklusintervall passen – „Puffer, damit sich
  Anfragen nicht überlagern“, Betreibervorgabe).
- `review()`: Schwellenwert `confidence >= 0.7` (selbst gemeldet vom Modell,
  keine unabhängige Verifikation – wird nur als grober Filter behandelt).
- **Kein Sonderweg für LLM-Funde:** Sie durchlaufen dieselbe
  `AuditStore.route()`-Logik wie deterministische Funde – ein einzelner
  Fehlurteil bleibt privat bei einem Agenten; erst ein zweiter, unabhängiger
  Treffer bei einem anderen Agenten macht es zu Gemeinschaftswissen.

## Tests

`tests/test_auditor_llm.py` – 16 Tests, alle mit gefälschtem HTTP-Opener (kein
echter Netzwerkaufruf in der Suite), aber mit den **echten, live erfassten
Antworten** von oben als Fixtures: Erschöpftes Denkbudget wird als `JudgeError`
behandelt statt abzustürzen; die echte Chronicler-Erkennung wird korrekt in ein
`AuditFinding` übersetzt; niedrige Konfidenz wird unterdrückt; Netzwerk-/
Parsing-Fehler werden sauber behandelt; Kandidatenauswahl überspringt bereits
erklärte Gründe und bereits geprüfte Events, begrenzt auf 5; LLM-Funde
durchlaufen nachweislich dasselbe Routing wie deterministische.

Gesamtsuite: 466 Tests, OK.

## Offen

- Periodischer Dienst (`village-auditor`-Erweiterung um den LLM-Batch-Aufruf,
  Zyklusintervall mit großzügigem Puffer, kein Überlappen laufender Anfragen).
- Verdrahtung von `invalid_decision_detail` in `web/runtime.py` (Telemetrie,
  damit Format-Fehler mit Volltext für Auditor/LLM verfügbar sind).
- systemd-Dienst, Memory-Gateway-Token für den Auditor, Prompt-Hinweis für die
  Agents, dass `village-auditor` eine System-Rolle ist.

## Nachtrag: Grübel-Problem bei mehrdeutigen/unproblematischen Fällen gefunden und behoben

Nach Bestätigung, dass `keep_alive=96h` korrekt funktioniert (Ladezeit im
warmen Zustand: 2,8–3,2 ms statt 60–150 s), zeigte ein absichtlich
uneindeutiger Testfall („ist das ok oder nicht?“) ein reales Problem:

- Mit dem ursprünglichen Prompt/Schema (600 Zeichen Feldlänge, keine
  Entscheidungsanweisung) geriet das Modell trotz `think:false` in
  ausuferndes, sich selbst widersprechendes Grübeln **innerhalb des
  `problem`-Feldstrings** – 400 Token verbraucht, `done_reason=length`,
  niemals eine schließende Klammer erreicht. Mein Code hätte das korrekt als
  `JudgeError` abgefangen (kein Absturz, kein Fund), aber ~70 s ohne Ergebnis.
- Fix: Feldlänge auf 300 Zeichen verschärft, Prompt um „Give ONE final verdict
  immediately, do not deliberate or second-guess yourself … Keep problem and
  solution to one short sentence each“ ergänzt.
- Mit dem geschärften Prompt/Schema lieferte derselbe Testfall in **18,4 s**
  eine saubere, korrekte `has_issue: false`-Antwort.

**Tests:** 2 neue (`PromptTighteningRegressionTests`), mit den echten
Antworten vor und nach der Korrektur als Fixtures. Gesamtsuite: 468 Tests, OK.
