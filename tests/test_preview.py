import asyncio
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from management import SessionManagementMixin
from session_store import SessionStore

class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class Plugin(SessionManagementMixin):
            async def _management_request(self, path, params=None):
                if self.offline:
                    raise RuntimeError('offline')
                self.params = params
                return {'ok': True, 'messages': [{'role':'user','content':'full text'}],
                        'next_cursor':'50:0','complete':False,'notice':'text only'}, 'base'
        self.p = Plugin()
        self.p.offline = False
        self.p.session_store = SessionStore(None)
        self.rec = self.p.session_store.upsert('tool:test','session-test','base')

    async def test_remote_cursor_and_content(self):
        out = await self.p.preview_managed_session(self.rec['id'], '20:0')
        self.assertEqual(self.p.params['cursor'], '20:0')
        self.assertEqual(out['next_cursor'], '50:0')
        self.assertEqual(out['source'], 'dsh')
        self.assertNotIn('base', out)

    async def test_webui_preview_auth_and_parameters(self):
        import types
        from unittest.mock import patch
        from webui import register_webui
        request = types.SimpleNamespace(username=None, query={"id": self.rec['id'], "cursor": "20:0"})
        routes = []
        self.p.context = types.SimpleNamespace(register_web_api=lambda *args: routes.append(args))
        with patch.dict(sys.modules, {"astrbot.api.web": types.SimpleNamespace(request=request)}):
            self.assertTrue(register_webui(self.p))
        handler = next(route[1] for route in routes if route[0].endswith('/sessions/preview'))
        self.assertEqual((await handler())["status"], "error")
        request.username = 'admin'
        out = await handler()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["data"]["source"], "dsh")
        self.assertEqual(self.p.params['cursor'], '20:0')

    async def test_fallback_not_complete_and_no_midstream_switch(self):
        self.p.offline = True
        out = await self.p.preview_managed_session(self.rec['id'])
        self.assertFalse(out['complete'])
        self.assertEqual(out['source'], 'local_fallback')
        with self.assertRaises(RuntimeError):
            await self.p.preview_managed_session(self.rec['id'], '20:0')

if __name__ == '__main__':
    unittest.main()
