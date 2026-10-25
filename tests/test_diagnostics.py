import importlib.util
import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import httpx

spec=importlib.util.spec_from_file_location('diag_test',Path(__file__).resolve().parents[1]/'diagnostics.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Logs(unittest.TestCase):
    def test_windows_retention_and_redaction(self):
        with tempfile.TemporaryDirectory() as d:
            h=m.DiagnosticLog(Path(d)/'logs.jsonl')
            now=time.time()
            for age,level,text in [(1,logging.INFO,'recent run'),(25,logging.INFO,'old run'),(50,logging.ERROR,'recent error'),(73,logging.ERROR,'old error'),(0,logging.ERROR,'Authorization: Bearer SECRET')]:
                r=logging.LogRecord('test',level,'',0,text,(),None);r.created=now-age*3600;h.emit(r)
            output=h.export()
            self.assertIn('recent run',output);self.assertIn('recent error',output)
            self.assertNotIn('old run',output);self.assertNotIn('old error',output);self.assertNotIn('SECRET',output)
    def test_url_validation(self):
        spec=importlib.util.spec_from_file_location('scan_diag',Path(__file__).resolve().parents[1]/'connection_scan.py')
        scan=importlib.util.module_from_spec(spec);spec.loader.exec_module(scan)
        for bad in ('http://[::1','http://localhost:bad','http://localhost:0','http://@localhost','http://localhost?','http://localhost#'):
            self.assertIsNone(scan.valid_base(bad),bad)
        self.assertEqual(scan.valid_base('http://[::1]:3188/'),'http://[::1]:3188')

class Connectivity(unittest.IsolatedAsyncioTestCase):
    async def test_auth_and_service_response(self):
        class P(m.DiagnosticsMixin):
            def _resolve_settings(self):return ['http://localhost:3188'],'private-token',30,1000
            def _runtime_bases(self,b):return ['http://localhost:3190']
        for status in (200,401,404):
            def handle(req):
                self.assertEqual(req.headers['authorization'],'Bearer private-token')
                body={'ok':True,'sessions':[]} if req.url.port==3188 else {'ok':True,'service':'dsh-runtime-observer'}
                return httpx.Response(status,json=body)
            original=httpx.AsyncClient
            def client(**kwargs):return original(transport=httpx.MockTransport(handle),**kwargs)
            with patch.object(m.httpx,'AsyncClient',client):
                p=P();result=await p.test_connectivity()
            self.assertEqual(result['ok'],status==200)
            self.assertEqual(len(result['results']),2)
            self.assertNotIn('private-token',json.dumps(result))
    async def test_empty_credentials(self):
        class P(m.DiagnosticsMixin):
            def _resolve_settings(self):return [],'',30,1000
        self.assertFalse((await P().test_connectivity())['ok'])
