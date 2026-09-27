# P33 (Groundwork) – HuggingFace-Metadaten-Anbindung, Option (a)

Date: 2026-09-27
Verification class: `LOCAL_VERIFIED`, funktional **inert** bis zur Netzwerkfreigabe.

## Entscheidung des Betreibers

Option (a) aus dem Plan: Netzwerk-Egress-Freigabe für einen gepinnten HuggingFace-Host,
analog zur bestehenden Wikipedia-Ausnahme. Diese Freigabe (Pi-/NAT-/Squid-Ebene) liegt
außerhalb meines Zugriffs und wurde in diesem Schritt **nicht** vorgenommen – nur der
Code, der sie nutzen würde, ist jetzt bereit.

## Bewusste Eingrenzung: Metadaten, nicht Inhalt

`village/research.py` bekommt eine neue Quelle `huggingface`, die ausschließlich
`https://huggingface.co/api/datasets/<org>/<name>` abfragt (Lizenz, Downloads, Tags,
Dateiliste) – **nie den Dataset-Inhalt selbst**. Das entspricht exakt dem bestehenden
Muster für `github`/`dockerhub` (auch dort werden nur Metadaten/Suchergebnisse geliefert,
keine Repository-Inhalte geklont). Ein tatsächlicher Datenimport bleibt ein separater,
manuell freigegebener Schritt (gepinnte Datei, Digest, Lizenzprüfung, `village-propose`),
wie im Plan (Abschnitt 4) beschrieben – das ist bewusst nicht Teil dieser Änderung.

## Umsetzung

- `village/research.py`: `ALLOWED_HOSTS["huggingface"] = ("huggingface.co",)`,
  Dataset-ID-Validierung per Regex vor jeder Anfrage (verhindert Pfad-Manipulation),
  Normalisierung auf `id, license, downloads, likes, tags, files, gated, private`.
- `web/decision.py`, `village/actions.py`: `huggingface` als vierte erlaubte
  `research_request`-Quelle.
- Prompt (voll) und README: `huggingface` dokumentiert, mit explizitem Hinweis, dass der
  Pfad ohne Firewall-Freigabe mit einem Netzwerkfehler fehlschlägt (fail-closed, keine
  Sicherheitslücke).

## Tests

`tests/test_research.py::HuggingFaceMetadataTests` (5 Tests, ausschließlich mit
`FakeResponse`-Mock, **kein echter Netzwerkzugriff**): Normalisierung inkl. Lizenz/Dateiliste,
Host-Beschränkung auf `huggingface.co`, Ablehnung eines manipulierten Datensatz-Namens vor
jeder Anfrage, `gated`/`private` werden angezeigt statt versteckt, kaputtes Payload-Format
führt zu leerem Ergebnis statt Absturz.

Gesamtsuite: 393 Tests, OK.

## Konkret zu entscheiden bleibt

1. **Firewall:** Der Betreiber muss `huggingface.co` auf der Pi-/Squid-Ebene freigeben;
   ich habe dazu keinen Zugriff. Bis dahin liefert jeder Versuch einen Netzwerkfehler.
2. **Datensatz-Auswahl für einen echten Import:** Die im Plan genannten Kandidaten
   (`NetoAISolutions/NetBench`, HuggingFaceTB-Reasoning-Kollektion) sind nicht vertieft
   geprüft (Lizenz im Detail, tatsächliche Eignung). Sobald die Firewall offen ist, kann
   ein Resident (oder ich, review-artig) `research_request(source=huggingface, query=...)`
   nutzen, um das selbst zu prüfen, bevor irgendein Import-Vorschlag entsteht.
