"""Only synthetic tokens; bridge behavior is independent of real accounts."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
from auth_bridge import AuthBridge


class AuthBridgeTests(unittest.TestCase):
    def setUp(self):
        self.client=[];self.native=[];self.calls=[]
        def tokens(**kwargs):
            self.calls.append(kwargs)
            return {'type':'chatgptAuthTokens','accessToken':'SYNTHETIC_ACCESS_'+str(len(self.calls)),
                    'chatgptAccountId':'synthetic-account','chatgptPlanType':'pro'}
        self.bridge=AuthBridge(tokens,lambda value:self.client.append(json.loads(value)),
                              lambda value:self.native.append(json.loads(value)))

    def login(self):
        self.bridge.client(b'{"id":1,"method":"initialize","params":{"clientInfo":{"name":"synthetic"}}}\n')
        self.bridge.server(b'{"id":1,"result":{"userAgent":"synthetic"}}\n')
        request=self.native[-1]
        self.assertEqual(request['method'],'account/login/start')
        self.assertEqual(self.client,[])
        self.bridge.server(json.dumps({'id':request['id'],'result':{'type':'chatgptAuthTokens'}}).encode())

    def test_login_before_desktop_initialization_complete_without_disk_auth(self):
        self.login()
        self.assertEqual(self.native[0]['params']['capabilities']['experimentalApi'],True)
        self.assertEqual(self.client,[{'id':1,'result':{'userAgent':'synthetic'}}])
        self.assertEqual(self.native[1]['method'],'initialized')
        self.bridge.client(b'{"method":"initialized"}')
        self.assertEqual(len(self.native),3)

    def test_refresh_callback_is_answered_by_owner_not_forwarded_to_desktop(self):
        self.login();self.client.clear()
        self.bridge.server(b'{"id":"refresh","method":"account/chatgptAuthTokens/refresh","params":{"previousAccountId":"synthetic-account"}}')
        result=self.native[-1]
        self.assertEqual(result['id'],'refresh')
        self.assertEqual(result['result']['accessToken'],'SYNTHETIC_ACCESS_2')
        self.assertNotIn('type',result['result'])
        self.assertEqual(self.calls[-1]['rejected_access_token'],'SYNTHETIC_ACCESS_1')
        self.assertEqual(self.client,[])

    def test_native_logout_is_local_and_does_not_silently_relogin(self):
        self.login();self.client.clear()
        self.bridge.client(b'{"id":2,"method":"account/logout","params":null}')
        self.assertEqual(self.native[-1]['method'],'account/logout')
        self.bridge.server(b'{"id":"refresh","method":"account/chatgptAuthTokens/refresh","params":{}}')
        self.assertIn('error',self.native[-1])
        self.assertEqual(len(self.calls),1)

    def test_direct_login_cannot_replace_the_harbor_account(self):
        self.login()
        before=len(self.native)
        self.bridge.client(b'{"id":3,"method":"account/login/start","params":{"type":"chatgpt"}}')
        self.assertIn('error',self.client[-1])
        self.assertEqual(len(self.native),before)

    def test_refresh_failure_does_not_leak_exception_or_credentials(self):
        self.login()
        def fail(**kwargs):raise ValueError('SYNTHETIC_PRIVATE_SECRET')
        self.bridge.credentials=fail
        self.bridge.server(b'{"id":4,"method":"account/chatgptAuthTokens/refresh","params":{}}')
        self.assertIn('error',self.native[-1])
        self.assertNotIn('SYNTHETIC_PRIVATE_SECRET',json.dumps(self.native[-1]))

    def test_regular_requests_and_tool_results_are_unchanged(self):
        self.login();self.client.clear()
        request=b'{"id":5,"method":"turn/start","params":{"input":[{"text":"ordinary"}]}}'
        self.bridge.client(request)
        self.assertEqual(self.native[-1],json.loads(request))
        result=b'{"id":5,"result":{"turn":{"id":"synthetic"}}}'
        self.bridge.server(result)
        self.assertEqual(self.client[-1],json.loads(result))


if __name__=='__main__':unittest.main()
