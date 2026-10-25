"""Metadata discovery uses only synthetic keys and localhost endpoints."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import model_metadata as metadata


class MetadataTests(unittest.TestCase):
    def test_context_normalization_and_invalid_input(self):
        for raw, expected in [('1,050,000',1050000),('１ ０５０ ０００ tokens',1050000),('1.05M',1050000),('128k',128000)]:
            self.assertEqual(metadata.context_number(raw), expected)
        for bad in ['-128000','1.2.3','10M','128000-256000',True,{},'unknown']:
            self.assertIsNone(metadata.context_number(bad))

    def test_sparse_metadata_never_infers_reasoning_wire_from_boolean(self):
        result=metadata.resolve('infi/example', {'context_length':'1,050,000','reasoning':True}, {})
        self.assertEqual(result['context'],1050000)
        self.assertEqual(result['context_source'],'router')
        self.assertEqual(result['reasoning'],'off')
        self.assertEqual(result['reasoning_source'],'preset')

    def test_explicit_reasoning_and_custom_values_are_validated(self):
        result=metadata.resolve('example', {'context_window':256000,
            'supported_reasoning_efforts':['low','high'], 'default_reasoning_effort':'high'}, {})
        self.assertEqual(result['levels'],['low','high'])
        self.assertEqual(result['effort'],'high')
        self.assertEqual(result['reasoning'],'standard')
        unsafe=metadata.resolve('example',{'supported_reasoning_efforts':['low'],
            'reasoning_field':'Authorization'}, {})
        self.assertEqual(unsafe['reasoning'],'off')

    def test_catalog_uses_actual_name_and_refuses_ambiguous_suffixes(self):
        catalog={'deepseek/example':1000000,'vendor/other':256000}
        result=metadata.resolve('infi/example',{},catalog)
        self.assertEqual(result['context'],1000000)
        self.assertEqual(result['context_source'],'catalog')
        self.assertEqual(result['catalog_model'],'deepseek/example')
        self.assertEqual(metadata.resolve('new/unknown',{},catalog)['context'],128000)
        self.assertEqual(metadata.resolve('infi/example',{},dict(catalog,**{'second/example':200000}))['context_source'],'preset')

    def test_exact_router_metadata_wins_and_duplicate_disagreement_is_unknown(self):
        rows=[{'id':'example','context_length':64000},{'id':'other','context_length':512000}]
        self.assertEqual(metadata.select_record({'data':rows},'example')['context_length'],64000)
        self.assertIsNone(metadata.select_record({'data':rows+[{'id':'example','context_length':128000}]},'example'))
        self.assertEqual(metadata.resolve('example',rows[0],{'vendor/example':512000})['context'],64000)

    def test_legacy_configuration_keeps_existing_values_without_writing(self):
        p=core.validated_profile({'name':'Test','kind':'api','url':'https://example.com/v1','protocol':'responses',
             'models':[{'alias':'gpt-6-luna','upstream':'unknown','context':200000,'reasoning':'off'}]})
        self.assertEqual(p['models'][0]['parameters']['manual'],['context','reasoning'])
        self.assertEqual(p['models'][0]['context'],200000)

    def test_metadata_cannot_echo_key_in_allowed_custom_value(self):
        from unittest.mock import patch, MagicMock
        response=MagicMock();response.__enter__.return_value=response;response.status=200
        response.read.return_value=json.dumps({'data':[{'id':'example','supported_reasoning_efforts':['low'],
            'effort_values':{'low':'SYNTHETIC_KEY'}}]}).encode()
        with patch('model_metadata.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=response
            result=metadata.fetch_worker(dict(url='https://example.com/v1',key='SYNTHETIC_KEY',models=['example']))
        self.assertNotIn('SYNTHETIC_KEY',json.dumps(result))
        self.assertEqual(result['models']['example']['reasoning'],'off')

    def test_get_is_bounded_redacted_and_does_not_follow_redirects(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                requests.append(self.path)
                if self.path.startswith('/redirect'):
                    self.send_response(302);self.send_header('Location','http://localhost:1/stolen');self.end_headers();return
                if self.path.startswith('/slow'):time.sleep(.4)
                raw=json.dumps({'data':[{'id':'example','context_length':256000}]}).encode()
                self.send_response(200);self.end_headers()
                try:self.wfile.write(raw)
                except BrokenPipeError:pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            result=metadata.discover(base,'SYNTHETIC_SECRET',['example'],timeout=2)
            self.assertEqual(result['models']['example']['context'],256000)
            self.assertNotIn('SYNTHETIC_SECRET',json.dumps(result))
            self.assertEqual(requests,['/models'])
            failed=metadata.discover(base+'/redirect','SYNTHETIC_SECRET',['unknown'],timeout=2)
            self.assertEqual(failed['status'],'http_error')
            self.assertEqual(failed['http_status'],302)
            began=time.monotonic()
            timed=metadata.discover(base+'/slow','SYNTHETIC_SECRET',['unknown'],timeout=.1)
            self.assertEqual(timed['status'],'timeout');self.assertLess(time.monotonic()-began,.5)
        finally:server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
