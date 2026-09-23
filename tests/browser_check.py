"""Optional browser acceptance test; requires chromium and websocket-client locally."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
import websocket

url = sys.argv[1] if len(sys.argv)>1 else 'http://192.168.155.226:8080'
with tempfile.TemporaryDirectory(prefix='village-browser-') as profile:
    proc = subprocess.Popen(['chromium','--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--no-proxy-server','--remote-allow-origins=*','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        portfile=Path(profile)/'DevToolsActivePort'
        for _ in range(100):
            if portfile.exists(): break
            time.sleep(.1)
        port=portfile.read_text().splitlines()[0]
        targets=json.load(urllib.request.urlopen('http://127.0.0.1:'+port+'/json'))
        ws=websocket.create_connection(next(t['webSocketDebuggerUrl'] for t in targets if t['type']=='page'),timeout=20)
        sequence=0; errors=[]; requests=[]
        def call(method,params=None):
            global sequence
            sequence+=1; ws.send(json.dumps({'id':sequence,'method':method,'params':params or {}}))
            while True:
                msg=json.loads(ws.recv())
                if msg.get('method')=='Runtime.exceptionThrown': errors.append(msg)
                if msg.get('method')=='Network.requestWillBeSent': requests.append(msg['params']['request']['url'])
                if msg.get('id')==sequence:
                    if 'error' in msg: raise RuntimeError(msg['error'])
                    return msg.get('result',{})
        def js(expression):
            return call('Runtime.evaluate',{'expression':expression,'returnByValue':True})['result'].get('value')
        def loaded():
            for _ in range(80):
                if js("document.querySelectorAll('.agent').length") == 9: return
                time.sleep(.15)
            raise AssertionError('Dashboard did not load nine agents')
        call('Runtime.enable');call('Network.enable');call('Page.enable')
        call('Emulation.setDeviceMetricsOverride',{'width':1500,'height':1100,'deviceScaleFactor':1,'mobile':False})
        call('Page.navigate',{'url':url+'/dashboard'});loaded()
        assert js("document.querySelectorAll('#habitat-map [data-node]').length") >= 16
        js("document.querySelector('[data-node=cpu]').dispatchEvent(new MouseEvent('click',{bubbles:true}))")
        assert 'CPU' in js("document.getElementById('map-inspector').innerText")
        js("document.querySelector('.agent').click()")
        assert js("document.getElementById('detail').open") is True
        js("document.getElementById('close-detail').click(); document.getElementById('pause').click()")
        assert js("document.getElementById('pause').getAttribute('aria-pressed')")=='true'
        js("document.getElementById('search').value='invalid_decision'; document.getElementById('search').dispatchEvent(new Event('input'))")
        assert js("document.getElementById('events').innerText.includes('Antwort verworfen')")
        shot=call('Page.captureScreenshot',{'format':'png'})['data'];Path('/tmp/ai-village-desktop.png').write_bytes(base64.b64decode(shot))
        for path in ('agents','habitat','timeline'):
            call('Page.navigate',{'url':url+'/'+path});loaded()
            assert js("document.querySelector('[aria-current=page]').getAttribute('href')")=='/'+path
        call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
        call('Page.navigate',{'url':url+'/dashboard'});loaded()
        assert js('document.documentElement.scrollWidth <= 390'), 'Mobile horizontal overflow'
        shot=call('Page.captureScreenshot',{'format':'png'})['data'];Path('/tmp/ai-village-mobile.png').write_bytes(base64.b64decode(shot))
        assert not errors, errors
        external=[r for r in requests if r.startswith(('http:','https:')) and not r.startswith(url+'/')]
        assert not external, external
        print(json.dumps({'agents':9,'hardware_map':True,'dialog':True,'filters':True,'navigation':True,'mobile_overflow':False,'javascript_errors':len(errors),'external_requests':len(external)}))
    finally:
        proc.terminate();proc.wait(timeout=10)
