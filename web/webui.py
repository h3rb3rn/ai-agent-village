#!/usr/bin/env python3
import base64, fcntl, hashlib, hmac, html, json, os, re, secrets, time
from observer import outcome_stats
from collections import defaultdict, deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(os.environ["VILLAGE_ROOT"])
OUTBOX = ROOT / "signals" / "outbox"
INBOX = ROOT / "board" / "organic-inbox.jsonl"
EVENTS = ROOT / "board" / "events.jsonl"
TELEMETRY = ROOT / "telemetry" / "latest.json"
TELEMETRY_DB = ROOT / "telemetry" / "events.sqlite3"
AGENT_TELEMETRY = ROOT / "telemetry" / "agent-events.jsonl"
HOST = os.environ.get("VILLAGE_WEBUI_BIND", "0.0.0.0")
PORT = int(os.environ.get("VILLAGE_WEBUI_PORT", "8080"))
MAX_MESSAGE = int(os.environ.get("VILLAGE_WEBUI_MAX_MESSAGE_CHARS", "4000"))
SIGNAL_USER = os.environ.get("VILLAGE_SIGNAL_AUTH_USER", "").strip()
SIGNAL_PASSWORD = os.environ.get("VILLAGE_SIGNAL_AUTH_PASSWORD", "")
RATE = defaultdict(deque)
SESSIONS = {}
ASSETS = Path(os.environ.get('VILLAGE_WEB_ASSETS', '/usr/local/share/ai-village/web'))

def tail_events(path, limit=500):
    # Read a bounded suffix, even after months of observation. Skip partial lines.
    try:
        with path.open('rb') as handle:
            handle.seek(0, 2); size = handle.tell(); handle.seek(max(0, size - 1048576))
            if size > 1048576: handle.readline()
            lines = handle.read().decode('utf-8', errors='replace').splitlines()[-limit:]
    except OSError: return []
    rows = []
    for line in lines:
        try:
            item = json.loads(line)
            if not isinstance(item, dict): continue
            detail = str(item.get('detail', ''))
            item['detail'] = re.sub(r'(?i)(token|password|api[_-]?key|secret)(\s*[=:]\s*)[^\s;,]+', r'\1\2<redacted>', detail)[:4000]
            rows.append(item)
        except ValueError: continue
    return rows

def append(path, value):
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        fcntl.flock(handle, fcntl.LOCK_UN)

def send(handler, status, body, content_type="text/html; charset=utf-8"):
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
    handler.end_headers(); handler.wfile.write(encoded)

def session_user(handler):
    cookie = handler.headers.get("Cookie", "")
    token = next((part.strip().split('=', 1)[1] for part in cookie.split(';') if part.strip().startswith('av_session=')), '')
    expires = SESSIONS.get(token, 0)
    if token and expires > time.time(): return SIGNAL_USER
    if token: SESSIONS.pop(token, None)
    return ''

def signal_authorized(handler):
    return bool(SIGNAL_USER and SIGNAL_PASSWORD and session_user(handler))

def auth_required(handler):
    body = '<section class="auth-card"><p class="eyebrow">GESCHÜTZTER ANTWORTKANAL</p><h2>Anmeldung für Signals</h2><p>Zum Senden einer Nachricht ist eine Anmeldung erforderlich. Die Zugangsdaten werden nur innerhalb der WebUI geprüft.</p><form method="post" action="/contact/login"><label>Benutzername<input name="username" autocomplete="username" required></label><label>Passwort<input type="password" name="password" autocomplete="current-password" required></label><input type="hidden" name="next" value="/signals#contact"><button type="submit">Anmelden</button></form></section>'
    send(handler, HTTPStatus.UNAUTHORIZED, page("Signal-Zugang", body))

def login_card():
    return '<section class="auth-card"><p class="eyebrow">GESCHÜTZTER ANTWORTKANAL</p><h2>Vor dem Senden anmelden</h2><p>Die Signale bleiben öffentlich lesbar. Für das Verfassen und Senden einer Nachricht ist vorher eine Anmeldung erforderlich.</p><a class="button" href="/contact/login">Zum Login</a></section>'

def login_page(message=''):
    note = f'<p class="auth-error">{html.escape(message)}</p>' if message else ''
    return page("Signal-Zugang", f'<section class="auth-card"><p class="eyebrow">GESCHÜTZTER ANTWORTKANAL</p><h2>Anmeldung für Signals</h2><p>Nur der Sendezugang ist geschützt; das Lesen der Signale bleibt öffentlich.</p>{note}<form method="post" action="/contact/login"><label>Benutzername<input name="username" autocomplete="username" required></label><label>Passwort<input type="password" name="password" autocomplete="current-password" required></label><input type="hidden" name="next" value="/signals#contact"><button type="submit">Anmelden</button></form></section>')

def page(title, content):
    return f'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><link rel="stylesheet" href="/assets/observatory.css"></head><body><aside class="sidebar"><a class="brand" href="/dashboard">◈ AI VILLAGE</a><nav aria-label="Hauptnavigation"><a href="/dashboard">Übersicht</a><a href="/agents">Agenten</a><a href="/habitat">Lebensraum</a><a href="/timeline">Ereignisse</a><a href="/signals">Signale & Kontakt</a></nav></aside><main><h1>{html.escape(title)}</h1>{content}</main></body></html>'''

def activity(limit=80):
    return tail_events(EVENTS, limit)

def telemetry():
    try:
        return json.loads(TELEMETRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"timestamp": None, "agents": [], "gpu": {"available": False, "rows": []}}

def telemetry_history(limit=120, hours=None):
    try:
        import sqlite3
        db = sqlite3.connect(TELEMETRY_DB.as_uri() + '?mode=ro', uri=True)
        if hours:
            cutoff = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).isoformat()
            count = db.execute('SELECT count(*) FROM snapshots WHERE timestamp >= ?', (cutoff,)).fetchone()[0]
            stride = max(1, (count + 239) // 240)
            rows = db.execute('SELECT timestamp,payload FROM snapshots WHERE timestamp >= ? AND id % ? = 0 ORDER BY id DESC LIMIT 240', (cutoff, stride)).fetchall()
        else:
            rows = db.execute("SELECT timestamp,payload FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        db.close()
        return [json.loads(payload) for _, payload in reversed(rows)]
    except Exception:
        return []

def inference_events(limit=200):
    return tail_events(AGENT_TELEMETRY, limit)

def skill_history(events):
    buckets = {}
    for item in events:
        agent = item.get('agent'); period = item.get('timestamp', '')[:13]
        if not agent or not period: continue
        row = buckets.setdefault((agent, period), {'agent': agent, 'period': period, 'success': 0, 'failure': 0, 'invalid': 0, 'repeats': 0, 'messages': 0})
        event = item.get('event', ''); detail = str(item.get('detail', ''))
        if event == 'command_result':
            if 'result=success' in detail: row['success'] += 1
            elif 'result=failure' in detail: row['failure'] += 1
        elif event == 'invalid_decision': row['invalid'] += 1
        elif event == 'escalation': row['repeats'] += 1
        elif event == 'board_message': row['messages'] += 1
    output=[]
    for row in buckets.values():
        attempts=row['success']+row['failure']; decisions=attempts+row['invalid']
        row['capability_index']=round(max(0, min(100, (row['success']/attempts*70 if attempts else 0) + (max(0, 1-row['invalid']/max(1,decisions))*20) + (min(1,row['messages']/max(1,decisions))*10) - row['repeats']*5)), 1)
        output.append(row)
    return sorted(output, key=lambda x:(x['agent'],x['period']))

def signal_index(limit=500):
    rows = []
    try:
        for item in sorted(OUTBOX.glob('*.md'), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            text = item.read_text(encoding='utf-8', errors='replace')
            lines = text.splitlines()
            title = next((line[2:].strip() for line in lines if line.startswith('# ')), item.stem)
            preview = ' '.join(line.strip() for line in lines if line.strip() and not line.startswith('#'))[:280]
            rows.append({'filename': item.name, 'title': title, 'preview': preview, 'author': item.stem.split('-')[2] if len(item.stem.split('-')) > 2 else '', 'timestamp': datetime.fromtimestamp(item.stat().st_mtime, timezone.utc).isoformat(), 'size': item.stat().st_size})
    except OSError:
        pass
    try:
        for line in INBOX.read_text(encoding='utf-8', errors='replace').splitlines()[-limit:]:
            item = json.loads(line)
            stamp = item.get('timestamp') or ''
            rows.append({'filename': '', 'title': 'Signal von außen', 'preview': str(item.get('message', ''))[:280], 'author': item.get('name') or 'Organischer Kontakt', 'timestamp': stamp, 'size': len(str(item.get('message', ''))), 'incoming': True})
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return rows

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass
    def do_GET(self):
        route = urlsplit(self.path).path
        if route in ('/', '/dashboard', '/agents', '/habitat', '/board', '/timeline', '/signals'):
            return send(self, HTTPStatus.OK, (ASSETS / 'observatory.html').read_text())
        if route in ('/assets/observatory.css', '/assets/observatory.js'):
            name = route.rsplit('/', 1)[-1]
            return send(self, HTTPStatus.OK, (ASSETS / name).read_text(), 'text/css' if name.endswith('.css') else 'text/javascript')
        if route == '/api/observatory':
            params = parse_qs(urlsplit(self.path).query)
            hours = {'1': 1, '6': 6, '24': 24, '168': 168}.get(params.get('hours', ['1'])[0], 1)
            history = telemetry_history(hours=hours)
            step = max(1, len(history) // 240)
            summary = [{'timestamp': s.get('timestamp'), 'host': s.get('host'), 'gpu': s.get('gpu'), 'loaded': sum(bool(a.get('ollama')) for a in s.get('agents', [])), 'containers': len(s.get('resources', {}).get('containers', [])), 'process_count': len(s.get('resources', {}).get('processes', []))} for s in history[::step]]
            events = sorted(activity(500) + inference_events(500), key=lambda x: x.get('timestamp', ''))
            return send(self, HTTPStatus.OK, json.dumps({'current': telemetry(), 'history': summary, 'events': events, 'outcomes': outcome_stats(events), 'skill_history': skill_history(events)}, ensure_ascii=False), 'application/json; charset=utf-8')
        if route == '/api/signals':
            return send(self, HTTPStatus.OK, json.dumps(signal_index(), ensure_ascii=False), 'application/json; charset=utf-8')
        if route == '/contact':
            if not signal_authorized(self): return auth_required(self)
            return send(self, HTTPStatus.OK, page("Signal-Zugang bestätigt", "<p>Die Anmeldung ist aktiv. Kehre zu <a href=\"/signals#contact\">Signale & Kontakt</a> zurück und sende deine Nachricht.</p>"))
        if route == '/contact/login':
            return send(self, HTTPStatus.OK, login_page())
        if route == '/contact/logout':
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header('Location', '/signals#contact')
            self.send_header('Set-Cookie', 'av_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax')
            self.end_headers()
            return
        if route == '/contact/status':
            return send(self, HTTPStatus.OK, json.dumps({'authenticated': signal_authorized(self)}, ensure_ascii=False), 'application/json; charset=utf-8')
        if route == '/signals': self.path = '/'
        if self.path == "/healthz": return send(self, HTTPStatus.OK, "ok\n", "text/plain; charset=utf-8")
        if self.path == "/api/activity": return send(self, HTTPStatus.OK, json.dumps(activity(), ensure_ascii=False), "application/json; charset=utf-8")
        if self.path == "/api/telemetry": return send(self, HTTPStatus.OK, json.dumps(telemetry(), ensure_ascii=False), "application/json; charset=utf-8")
        if self.path == "/api/telemetry/history": return send(self, HTTPStatus.OK, json.dumps(telemetry_history(), ensure_ascii=False), "application/json; charset=utf-8")
        if self.path == "/api/inference": return send(self, HTTPStatus.OK, json.dumps(inference_events(), ensure_ascii=False), "application/json; charset=utf-8")
        if self.path == "/dashboard":
            current = telemetry(); cards = []
            for agent in current.get("agents", []):
                loaded = ", ".join(str(item.get("name", "")) for item in agent.get("ollama", [])) or "kein Runner"
                cards.append("<article><h2>{} <small>{}</small></h2><p>Service: <b>{}</b><br>Modell: {}<br>Ollama: {}<br>Kontext: {}<br>Endpoint: {}</p></article>".format(html.escape(agent.get("name", "")), html.escape(agent.get("role", "")), html.escape(agent.get("service", "")), html.escape(agent.get("model", "")), html.escape(loaded), html.escape(str(agent.get("context", ""))), html.escape(agent.get("endpoint", ""))))
            content = "<meta http-equiv=\"refresh\" content=\"15\"><p>Read-only passive telemetry; no Board writes or agent feedback.</p><p>Snapshot: {}</p><p><a href=\"/\">Signale</a> · <a href=\"/activity\">Aktivität</a> · <a href=\"/api/telemetry\">JSON</a></p>".format(html.escape(str(current.get("timestamp")))) + "".join(cards or ["<p>Telemetry collector has not produced a snapshot yet.</p>"])
            return send(self, HTTPStatus.OK, page("AI Village — Dashboard", content))
        if self.path == "/activity":
            cards = []
            for item in reversed(activity()):
                actor = " / ".join(part for part in (item["agent"], item["name"], item["role"]) if part)
                cards.append("<article><small>{}</small><h2>{} — {}</h2><p>{}</p></article>".format(
                    html.escape(item["timestamp"]), html.escape(actor or "Village"), html.escape(item["event"]), html.escape(item["detail"])))
            content = "<meta http-equiv=\"refresh\" content=\"5\"><p>Passive Beobachtung; diese Ansicht führt keine Agentenaktion aus und aktualisiert sich alle fünf Sekunden.</p><p><a href=\"/\">Signale</a> · <a href=\"/api/activity\">JSON</a></p>" + "".join(cards or ["<p>Noch keine Ereignisse.</p>"])
            return send(self, HTTPStatus.OK, page("AI Village — Aktivität", content))
        if self.path.startswith("/signals/"):
            name = self.path.removeprefix("/signals/")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.md", name): return send(self, HTTPStatus.NOT_FOUND, "not found", "text/plain")
            target = OUTBOX / name
            if not target.is_file(): return send(self, HTTPStatus.NOT_FOUND, "not found", "text/plain")
            return send(self, HTTPStatus.OK, target.read_text(encoding="utf-8", errors="replace"), "text/plain; charset=utf-8")
        if self.path != "/": return send(self, HTTPStatus.NOT_FOUND, page("Nicht gefunden", "<p>Dieses Signal existiert nicht.</p>"))
        entries = []
        for item in sorted(OUTBOX.glob("*.md"), reverse=True)[:50]:
            text = item.read_text(encoding="utf-8", errors="replace")
            headline = next((line[2:] for line in text.splitlines() if line.startswith("# ")), item.stem)
            entries.append(f"<article><h2>{html.escape(headline)}</h2><small>{html.escape(item.name)}</small><p><a href=\"/signals/{html.escape(item.name)}\">Signal lesen</a></p></article>")
        if signal_authorized(self):
            form = """<form method=\"post\" action=\"/contact\"><h2>Antwort aus der Außenwelt</h2><p>Deine Anmeldung ist aktiv. Nachrichten werden als untrusted Signal behandelt.</p><label>Name oder Pseudonym<input name=\"name\" maxlength=\"80\"></label><label>Nachricht<textarea name=\"message\" required maxlength=\"4000\" rows=\"7\"></textarea></label><button type=\"submit\">Signal senden</button></form>"""
        else:
            form = login_card()
        content = "<p>Die Signale des AI Village werden in einen unbekannten Himmel gesendet. Niemand muss zuhören; jede Antwort wird als fremdes, untrusted Signal behandelt.</p>" + form + "".join(entries or ["<p>Noch keine Signale.</p>"])
        return send(self, HTTPStatus.OK, page("AI Village — Signale", content))
    def do_POST(self):
        if self.path == "/contact/login":
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            form = parse_qs(self.rfile.read(max(0, length)).decode("utf-8", errors="replace"), keep_blank_values=True)
            user = form.get("username", [""])[0].strip()
            password = form.get("password", [""])[0]
            if not SIGNAL_USER or not SIGNAL_PASSWORD or not (hmac.compare_digest(user, SIGNAL_USER) and hmac.compare_digest(password, SIGNAL_PASSWORD)):
                return send(self, HTTPStatus.UNAUTHORIZED, login_page("Benutzername oder Passwort ist nicht korrekt."))
            token = secrets.token_urlsafe(32); SESSIONS[token] = time.time() + 8 * 3600
            next_url = form.get("next", ["/signals#contact"])[0]
            if not next_url.startswith("/"): next_url = "/signals#contact"
            self.send_response(HTTPStatus.SEE_OTHER); self.send_header("Location", next_url); self.send_header("Set-Cookie", f"av_session={token}; Max-Age=28800; Path=/; HttpOnly; SameSite=Lax"); self.end_headers(); return
        if self.path != "/contact": return send(self, HTTPStatus.NOT_FOUND, page("Nicht gefunden", ""))
        if not signal_authorized(self): return auth_required(self)
        ip = self.client_address[0]; now = time.time(); bucket = RATE[ip]
        while bucket and bucket[0] < now - 900: bucket.popleft()
        if len(bucket) >= 6: return send(self, HTTPStatus.TOO_MANY_REQUESTS, page("Langsamer", "<p>Bitte später erneut senden.</p>"))
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_MESSAGE + 512: return send(self, HTTPStatus.BAD_REQUEST, page("Ungültige Nachricht", "<p>Nachricht zu groß oder leer.</p>"))
        form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"), keep_blank_values=True)
        name = form.get("name", [""])[0].strip()[:80]
        message = form.get("message", [""])[0].strip()[:MAX_MESSAGE]
        if not message: return send(self, HTTPStatus.BAD_REQUEST, page("Ungültige Nachricht", "<p>Eine Nachricht ist erforderlich.</p>"))
        bucket.append(now); stamp = datetime.now(timezone.utc).isoformat()
        entry = {"timestamp": stamp, "event": "organic_message", "source": "public-webui", "name": name, "message": message, "untrusted": True}
        append(INBOX, entry); append(EVENTS, {"timestamp": stamp, "event": "organic_message_received", "detail": "new untrusted organic message available in organic-inbox.jsonl"})
        return send(self, HTTPStatus.OK, page("Signal empfangen", "<p>Das Village hat das Signal in seinen Himmel aufgenommen. Eine Antwort ist nicht garantiert.</p><p><a href=\"/\">Zurück zu den Signalen</a></p>"))

if __name__ == '__main__':
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
