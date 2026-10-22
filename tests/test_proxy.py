import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
from proxy import Gateway
from test_core import api


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.requests=[]
        self.status=200
        owner=self
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((self.path,dict(self.headers),body))
                self.send_response(owner.status)
                self.send_header('Content-Type','text/event-stream')
                if owner.status==307:self.send_header('Location',owner.base+'/credential-trap')
                self.end_headers()
                self.wfile.write(b'data: {"type":"response.completed","response":{"id":"r","output":[]}}\n\n')
            def log_message(self,*args): pass
        self.upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        self.base=f'http://127.0.0.1:{self.upstream.server_port}'
        threading.Thread(target=self.upstream.serve_forever,daemon=True).start()
        self.profile=api(); self.profile.update(url=self.base+'/v1',id='a'*32)
        self.events=[]
        self.gateway=Gateway(self.profile,'SYNTHETIC_UPSTREAM_KEY',self.events.append)
        self.gateway.start()

    def tearDown(self):
        self.gateway.close();self.upstream.shutdown();self.upstream.server_close()

    def request(self,model='gpt-6-astra',key=None):
        body=json.dumps({'model':model,'input':'SYNTHETIC_HISTORY','stream':True}).encode()
        return urllib.request.urlopen(urllib.request.Request(self.gateway.url+'/responses',data=body,
            headers={'Authorization':'Bearer '+(key or self.gateway.token),'Content-Type':'application/json',
                     'ChatGPT-Account-ID':'MUST_NOT_FORWARD','Cookie':'MUST_NOT_FORWARD'}),timeout=3)

    def test_mapping_credentials_and_stream(self):
        with self.request() as r: self.assertIn(b'response.completed',r.read())
        path,headers,body=self.requests[0]
        self.assertEqual(path,'/v1/responses')
        self.assertEqual(body['model'],'infi/glm-5.3')
        lower={k.lower():v for k,v in headers.items()}
        self.assertEqual(lower['authorization'],'Bearer SYNTHETIC_UPSTREAM_KEY')
        self.assertNotIn('chatgpt-account-id',lower)
        self.assertNotIn('cookie',lower)
        self.assertNotIn('SYNTHETIC_UPSTREAM_KEY',json.dumps(self.events))
        self.assertNotIn('SYNTHETIC_HISTORY',json.dumps(self.events))

    def test_bad_auth_or_unknown_model_never_reaches_upstream(self):
        for kwargs in [{'key':'WRONG'},{'model':'not-mapped'}]:
            with self.assertRaises(urllib.error.HTTPError) as caught: self.request(**kwargs)
            caught.exception.close()
        self.assertEqual(self.requests,[])

    def test_redirect_is_not_followed_and_failure_not_returned_as_success(self):
        self.status=307
        with self.assertRaises(urllib.error.HTTPError) as caught: self.request()
        caught.exception.close()
        self.assertEqual(len(self.requests),1)

    def test_does_not_proxy_unlisted_endpoints(self):
        req=urllib.request.Request(self.gateway.url+'/arbitrary',data=b'{}',
                                  headers={'Authorization':'Bearer '+self.gateway.token})
        with self.assertRaises(urllib.error.HTTPError) as caught: urllib.request.urlopen(req,timeout=2)
        caught.exception.close()
        self.assertEqual(self.requests,[])

    def test_native_websocket_probe_falls_back_without_contacting_upstream(self):
        for token,status in ((self.gateway.token,426),('WRONG',401)):
            req=urllib.request.Request(self.gateway.url+'/responses',headers={
                'Authorization':'Bearer '+token,'Upgrade':'websocket','Connection':'Upgrade'})
            with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(req,timeout=2)
            with caught.exception as error:self.assertEqual(error.code,status)
        self.assertEqual(self.requests,[])

    def test_logging_failure_does_not_exhaust_request_slots(self):
        def fail(_):raise OSError('synthetic disk full')
        self.gateway.record=fail
        for _ in range(10):
            with self.request() as response:self.assertIn(b'response.completed',response.read())
        self.assertEqual(len(self.requests),10)


if __name__=='__main__':unittest.main()
