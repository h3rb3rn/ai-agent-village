import json, os, tempfile, threading, unittest
from http.client import HTTPConnection
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'memory'))
import gateway

class MemoryGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); gateway.DB = Path(self.tmp.name) / 'memory.sqlite3'; gateway.TOKEN = 'test'; gateway.RATE_LIMIT = 2
        self.server = gateway.ThreadingHTTPServer(('127.0.0.1', 0), gateway.Handler); threading.Thread(target=self.server.serve_forever, daemon=True).start(); self.port = self.server.server_address[1]
    def tearDown(self): self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()
    def call(self, method, path, body=None, token='test'):
        c=HTTPConnection('127.0.0.1', self.port); raw=None if body is None else json.dumps(body); c.request(method, path, raw, {'Content-Type':'application/json','Authorization':f'Bearer {token}'}); r=c.getresponse(); return r.status, json.loads(r.read())
    def test_provenance_search_and_quota(self):
        status, item = self.call('POST','/v1/memories', {'agent':'artisan','scope':'private','kind':'observation','content':'GPU query needs nounits','source_event':'evt-1','confidence':.9}); self.assertEqual(status,201); self.assertEqual(item['source_event'],'evt-1')
        status, result = self.call('POST','/v1/search', {'query':'GPU nounits','agent':'artisan'}); self.assertEqual(status,200); self.assertEqual(result['items'][0]['agent'],'artisan')
        self.call('POST','/v1/memories', {'agent':'artisan','content':'second'}); status, _ = self.call('POST','/v1/memories', {'agent':'artisan','content':'third'}); self.assertEqual(status,429)
    def test_authentication(self): self.assertEqual(self.call('GET','/v1/memories', token='wrong')[0],401)

if __name__ == '__main__': unittest.main()
