#!/usr/bin/env python3
"""Small, dependency-free memory gateway.

SQLite is the authoritative, rebuildable journal. Chroma and Neo4j are optional
projections configured through environment variables; agents never receive
database credentials or direct database network access.
"""
import hashlib, json, os, re, sqlite3, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(os.environ.get('VILLAGE_ROOT', '/var/lib/ai-village'))
DB = Path(os.environ.get('MEMORY_DB', ROOT / 'memory' / 'memory.sqlite3'))
HOST = os.environ.get('MEMORY_BIND', '127.0.0.1')
PORT = int(os.environ.get('MEMORY_PORT', '8090'))
TOKEN = os.environ.get('MEMORY_GATEWAY_TOKEN', '')
MAX_CONTENT = int(os.environ.get('MEMORY_MAX_CONTENT_CHARS', '12000'))
MAX_RESULTS = int(os.environ.get('MEMORY_MAX_RESULTS', '12'))
RATE_LIMIT = int(os.environ.get('MEMORY_WRITES_PER_HOUR', '120'))

def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS memories (
      id TEXT PRIMARY KEY, created_at TEXT NOT NULL, agent TEXT NOT NULL,
      scope TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
      source_event TEXT, confidence REAL, expires_at TEXT, metadata TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS memories_agent_time ON memories(agent, created_at)')
    conn.commit(); return conn

def now(): return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
def tokens(text): return set(re.findall(r'[a-z0-9äöüß_-]{3,}', text.lower()))
def auth(handler):
    return not TOKEN or handler.headers.get('Authorization', '') == f'Bearer {TOKEN}'
def json_body(handler):
    length = int(handler.headers.get('Content-Length', '0'))
    if length <= 0 or length > MAX_CONTENT + 8192: raise ValueError('invalid body size')
    value = json.loads(handler.rfile.read(length).decode('utf-8'))
    if not isinstance(value, dict): raise ValueError('JSON object required')
    return value
def result(row):
    item = dict(row); item['metadata'] = json.loads(item['metadata']); return item

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body))); self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff'); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if urlsplit(self.path).path == '/healthz': return self.send_json(200, {'ok': True, 'backend': 'sqlite-authoritative'})
        if not auth(self): return self.send_json(401, {'error': 'unauthorized'})
        if urlsplit(self.path).path == '/v1/memories':
            conn = db(); rows = conn.execute('SELECT * FROM memories ORDER BY created_at DESC LIMIT ?', (MAX_RESULTS,)).fetchall(); conn.close()
            return self.send_json(200, {'items': [result(r) for r in rows]})
        if urlsplit(self.path).path == '/v1/stats':
            conn = db(); rows = conn.execute('SELECT agent, count(*) AS memories, coalesce(sum(length(content)),0) AS chars, max(created_at) AS last_at FROM memories GROUP BY agent ORDER BY agent').fetchall(); conn.close()
            return self.send_json(200, {'agents': [dict(r) for r in rows], 'total': sum(r['memories'] for r in rows), 'chars': sum(r['chars'] for r in rows)})
        return self.send_json(404, {'error': 'not found'})
    def do_POST(self):
        if not auth(self): return self.send_json(401, {'error': 'unauthorized'})
        route = urlsplit(self.path).path
        try: value = json_body(self)
        except (ValueError, json.JSONDecodeError) as exc: return self.send_json(400, {'error': str(exc)})
        if route == '/v1/memories':
            content = str(value.get('content', '')).strip(); agent = str(value.get('agent', '')).strip()
            if not content or len(content) > MAX_CONTENT or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,80}', agent): return self.send_json(400, {'error': 'content or agent invalid'})
            conn = db(); cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time()-3600)); count = conn.execute('SELECT count(*) FROM memories WHERE agent=? AND created_at>=?', (agent, cutoff)).fetchone()[0]
            if count >= RATE_LIMIT: conn.close(); return self.send_json(429, {'error': 'agent memory write quota exceeded'})
            created = now(); ident = hashlib.sha256(f'{agent}\0{created}\0{content}'.encode()).hexdigest()[:24]
            item = {'id': ident, 'created_at': created, 'agent': agent, 'scope': str(value.get('scope', 'private'))[:32], 'kind': str(value.get('kind', 'observation'))[:32], 'content': content, 'source_event': str(value.get('source_event', ''))[:200], 'confidence': float(value.get('confidence', 0.5)), 'expires_at': str(value.get('expires_at', ''))[:40], 'metadata': json.dumps(value.get('metadata', {}), ensure_ascii=False) if isinstance(value.get('metadata', {}), dict) else '{}'}
            conn.execute('INSERT OR IGNORE INTO memories VALUES (:id,:created_at,:agent,:scope,:kind,:content,:source_event,:confidence,:expires_at,:metadata)', item); conn.commit(); row = conn.execute('SELECT * FROM memories WHERE id=?', (ident,)).fetchone(); conn.close()
            return self.send_json(201, result(row))
        if route == '/v1/search':
            query = str(value.get('query', '')).strip(); scope = value.get('scope'); agent = value.get('agent'); limit = min(MAX_RESULTS, max(1, int(value.get('limit', 8))))
            if not query: return self.send_json(400, {'error': 'query required'})
            conn = db(); clauses=[]; args=[]
            if scope: clauses.append('scope=?'); args.append(str(scope))
            if agent: clauses.append('agent=?'); args.append(str(agent))
            where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
            rows = [result(r) for r in conn.execute('SELECT * FROM memories'+where+' ORDER BY created_at DESC LIMIT 500', args).fetchall()]; conn.close()
            wanted=tokens(query)
            for item in rows: item['_score']=len(wanted & tokens(item['content'])) / max(1, len(wanted))
            rows=sorted((x for x in rows if x['_score']>0), key=lambda x:(x['_score'],x['created_at']), reverse=True)[:limit]
            for x in rows: x['score']=x.pop('_score')
            return self.send_json(200, {'query': query, 'items': rows, 'backend': 'sqlite-lexical-fallback'})
        return self.send_json(404, {'error': 'not found'})

if __name__ == '__main__':
    db().close(); ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
