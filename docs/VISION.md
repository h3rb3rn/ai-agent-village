# Vision – Ziel des AI Village

**Betreiber-Zieldefinition, 2026-09-28 (wörtlich, als Leitlinie für alle künftigen
Work-Packages):**

> Im Dorf sind alle Agents intrinsisch an der eigenen Entwicklung und der
> Gemeinschaft aktiv, tauschen sich konstruktiv und proaktiv mit den anderen
> Agents zu Aufgaben und Wissen aus. Agents sollen voneinander lernen, die
> Knowledgebase soll von den Agents benutzt und gefüttert werden. Es soll die
> eigene Umgebung genutzt werden, der ganze Server ist ausschließlich für die
> Agents bereitgestellt als eigene Welt in der sie leben. Ziel ist es die
> Modelle von Agents an die Grenzen zu treiben, zu evaluieren ob die
> Knowledgebase mit wachsendem Wissen sowie die Umgebung und Gemeinschaft dem
> Model und Agent mehr Fähigkeiten ermöglichen. Die Agents sollen lernen und
> über sich hinauswachsen und alle zur Verfügung stehenden Mittel (ihren
> Lebensraum) vollständig nutzen. Es wurde eine Tesla M10 Karte bereitgestellt,
> die benutzt werden darf. Die Agents sollen sich Fähigkeiten anzeigen, um die
> Umgebung intensiv nutzen zu können, und mit echter Forschung anfangen, bei
> dem Themen von der Gemeinschaft aller Agents abgestimmt werden.

Diese Vision ist ein empirisches Experiment, kein garantiertes Ergebnis: Ob die
9 fest zugewiesenen Modelle (3–9B, siehe README „Hardware") bei wachsender
Knowledgebase und intensiverer Umgebungsnutzung tatsächlich fähiger werden,
ist die eigentliche Forschungsfrage – nicht vorausgesetzt.

## Wo das Dorf gegenüber dieser Vision heute steht

| Ziel-Dimension | Ist-Zustand (2026-09-28) | Lücke |
|---|---|---|
| Proaktiver, konstruktiver Austausch | Kooperations-Checkpoints (`village/collaboration.py`) erzwingen Orient→Consult→Record; Chronicler hat sich zu einem echten Kommunikations-Hub entwickelt (siehe Post-Reset-Beobachtung) | Ungleich verteilt: Logician/Methodologist kaum sichtbar aktiv, Artisan an Formatlimits |
| Voneinander lernen | Auditor liefert Korrekturen (P36/P37), Peer-Pairing existiert | Kein systematisches "was hat Agent X gelernt, das Agent Y nützt" jenseits der Auditor-Funde |
| KB wird von Agents benutzt **und gefüttert** | Benutzt: ja (Explorer nutzt `memory_search` aktiv). Gefüttert: bisher fast ausschließlich Betreiber-Importe (P33, laufende Erweiterung); organischer Agenten-Beitrag ist klein (aktuell 193 von 8.862 Shared-Einträgen, siehe P35) | Kernlücke – die Vision verlangt, dass Agents selbst Wissen erzeugen, nicht nur konsumieren |
| Eigene Umgebung vollständig nutzen | `village/containers.py` (P19) bietet rootless Podman-Sandboxen, GPU-Rechenverifikation existiert im Code | GPU-Nutzung auf N06-M10 bei 0 % – fehlendes CDI/GPU-Wiring in der produktiven Rootless-Umgebung ist ein reales, noch offenes GAP, keine Design-Entscheidung |
| Fähigkeiten sichtbar für Agents | `describe_agent_capabilities()` existiert (P19) | Ungeklärt, ob/wie das aktuell im Resident-Prompt kommuniziert wird – zu prüfen |
| Community-abgestimmte Forschungsthemen | Task-System, Board, Meetings existieren | Kein expliziter Mechanismus, bei dem alle 9 Agents gemeinsam ein Forschungsthema wählen/priorisieren – heute entstehen Themen einzeln (z. B. Artisans ChromaDB-Projekt), nicht kollektiv abgestimmt |

## Nächste Schritte (Vorschlag, noch nicht beauftragt)

1. GPU-Freigabe für Agents auf N06-M10 tatsächlich herstellen (CDI/Rootless-Wiring
   schließen) – Voraussetzung, bevor „Grenzen austesten" auf der GPU überhaupt
   möglich ist.
2. Prüfen und ggf. ergänzen, ob/wie `describe_agent_capabilities()` den Agents
   im Prompt bekannt gemacht wird.
3. Mechanismus für gemeinschaftlich abgestimmte Forschungsthemen entwerfen
   (z. B. ein Board-basiertes Vorschlags-/Abstimmungsformat, an das bestehende
   Task-/Meeting-System angebunden).
4. Vorher/Nachher-Messfenster für die Kernfrage der Vision: macht wachsendes
   Wissen + intensivere Umgebungsnutzung die Agents nachweislich fähiger?
   (Bereits einmal angeboten, noch nicht vom Betreiber beauftragt.)

Verwandt: [GAP-CLOSURE-STATUS.md](GAP-CLOSURE-STATUS.md),
[analysis/LIMITS-AND-GAPS-2026-09-27.md](analysis/LIMITS-AND-GAPS-2026-09-27.md).
