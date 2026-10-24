"""Isolated ConnectionScanMixin tests: no AstrBot imports or real network."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'connection_scan_isolated', ROOT / 'connection_scan.py')
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


class Config(dict):
    def __init__(self, values):
        super().__init__(values)
        self.save_config = Mock()


class Plugin(scan.ConnectionScanMixin):
    def __init__(self, bases=None, token='current-secret-token'):
        self.bases = list(bases if bases is not None else ['https://configured.example'])
        self.current = token
        self.config = Config({'ingress_url': 'https://old.example',
                              'token': 'old-secret-token', 'other_setting': {'keep': True}})
        self._probe_connection = AsyncMock(return_value=False)

    def _resolve_settings(self):
        return self.bases, self.current, None, None


class ConnectionScanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000.0
        self.local_bases = ('http://127.0.0.1:3188', 'http://host.docker.internal:3188')
        self.network = self.enterContext(patch.object(
            scan.httpx, 'AsyncClient', side_effect=AssertionError('Real network forbidden')))
        # Patch the module's clock reference, not the event loop's global clock.
        self.enterContext(patch.object(
            scan, 'time', SimpleNamespace(monotonic=lambda: self.now)))
        self.enterContext(patch.object(scan, 'TOKEN_PATHS', ()))
        self.enterContext(patch.object(scan, 'LOCAL_BASES', self.local_bases))
        self.addCleanup(self.network.assert_not_called)

    async def discover_one(self, plugin):
        plugin._probe_connection.side_effect = (
            lambda base, token: base == plugin.bases[0] and token == plugin.current)
        result = await plugin.scan_connections()
        self.assertEqual(len(result['candidates']), 1)
        return result['candidates'][0]

    async def test_current_credentials_discovered_but_response_redacts_token(self):
        plugin = Plugin()
        before = dict(plugin.config)
        candidate = await self.discover_one(plugin)
        self.assertEqual(candidate['ingress_url'], plugin.bases[0])
        self.assertTrue(candidate['id'])
        self.assertEqual(candidate['token'], '已找到（不显示）')
        self.assertIn('当前', candidate['source'])
        self.assertNotIn(plugin.current, json.dumps(candidate, ensure_ascii=False))
        self.assertEqual(plugin._connection_scan_pending[candidate['id']][:2],
                         (plugin.bases[0], plugin.current))
        self.assertEqual(dict(plugin.config), before)
        plugin.config.save_config.assert_not_called()
        self.assertEqual(plugin._probe_connection.await_args_list[0].args,
                         (plugin.bases[0], plugin.current))

    async def test_apply_revalidates_and_persists_config_then_consumes_results(self):
        plugin = Plugin()
        candidate = await self.discover_one(plugin)
        plugin._probe_connection.reset_mock()
        plugin._probe_connection.side_effect = None
        plugin._probe_connection.return_value = True
        saved_states = []
        plugin.config.save_config.side_effect = lambda: saved_states.append(dict(plugin.config))

        result = await plugin.apply_connection_scan(candidate['id'])

        plugin._probe_connection.assert_awaited_once_with(plugin.bases[0], plugin.current)
        plugin.config.save_config.assert_called_once_with()
        self.assertEqual(saved_states[0]['ingress_url'], plugin.bases[0])
        self.assertEqual(saved_states[0]['token'], plugin.current)
        self.assertEqual(saved_states[0]['other_setting'], {'keep': True})
        self.assertTrue(result['saved'])
        self.assertTrue(result['experimental'])
        self.assertEqual(result['ingress_url'], plugin.bases[0])
        self.assertNotIn(plugin.current, json.dumps(result, ensure_ascii=False))
        self.assertEqual(plugin._connection_scan_pending, {})
        with self.assertRaisesRegex(ValueError, 'expired scan'):
            await plugin.apply_connection_scan(candidate['id'])
        plugin.config.save_config.assert_called_once()

    async def test_apply_without_scan_or_with_unknown_identifier_rejected(self):
        plugin = Plugin()
        with self.assertRaisesRegex(ValueError, 'scan required'):
            await plugin.apply_connection_scan('not-issued')
        await self.discover_one(plugin)
        plugin._probe_connection.reset_mock()
        with self.assertRaisesRegex(ValueError, 'expired scan'):
            await plugin.apply_connection_scan('not-issued')
        plugin._probe_connection.assert_not_awaited()
        plugin.config.save_config.assert_not_called()

    async def test_expired_candidate_rejected_without_probe_or_save(self):
        plugin = Plugin()
        candidate = await self.discover_one(plugin)
        before = dict(plugin.config)
        plugin._probe_connection.reset_mock()
        self.now += 301
        with self.assertRaisesRegex(ValueError, 'expired scan'):
            await plugin.apply_connection_scan(candidate['id'])
        plugin._probe_connection.assert_not_awaited()
        plugin.config.save_config.assert_not_called()
        self.assertEqual(dict(plugin.config), before)

    async def test_failed_revalidation_rejects_unchanged_config(self):
        plugin = Plugin()
        candidate = await self.discover_one(plugin)
        before = dict(plugin.config)
        plugin._probe_connection.reset_mock()
        plugin._probe_connection.side_effect = None
        plugin._probe_connection.return_value = False
        with self.assertRaisesRegex(ValueError, 'connection changed'):
            await plugin.apply_connection_scan(candidate['id'])
        plugin._probe_connection.assert_awaited_once_with(plugin.bases[0], plugin.current)
        plugin.config.save_config.assert_not_called()
        self.assertEqual(dict(plugin.config), before)

    async def test_cooldown_blocks_scan_until_ten_seconds(self):
        plugin = Plugin()
        candidate = await self.discover_one(plugin)
        plugin._probe_connection.reset_mock()
        self.now += 9.999
        with self.assertRaisesRegex(ValueError, 'scan cooldown'):
            await plugin.scan_connections()
        plugin._probe_connection.assert_not_awaited()
        self.assertIn(candidate['id'], plugin._connection_scan_pending)
        self.now = 1010.0
        result = await plugin.scan_connections()
        self.assertEqual(len(result['candidates']), 1)
        self.assertNotIn(candidate['id'], plugin._connection_scan_pending)

    async def test_concurrent_scan_rejected_before_probing(self):
        plugin = Plugin()
        plugin._connection_scan_lock = asyncio.Lock()
        async with plugin._connection_scan_lock:
            with self.assertRaisesRegex(ValueError, 'scan in progress'):
                await plugin.scan_connections()
        plugin._probe_connection.assert_not_awaited()

    async def test_file_token_only_probed_locally_never_sent_to_remote_bases(self):
        plugin = Plugin(bases=['https://remote.example', 'http://another-remote.example:3188'])
        file_token = 'discovered-file-secret-token'
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / 'config.json'
            token_path.write_text(json.dumps({'token': file_token}), encoding='utf-8')
            with patch.object(scan, 'TOKEN_PATHS', (token_path,)):
                plugin._probe_connection.side_effect = lambda base, token: token == file_token
                result = await plugin.scan_connections()

        calls = [call.args for call in plugin._probe_connection.await_args_list]
        for remote in plugin.bases:
            self.assertIn((remote, plugin.current), calls)
            self.assertNotIn((remote, file_token), calls)
        for local in self.local_bases:
            self.assertIn((local, file_token), calls)
        self.assertEqual({c['ingress_url'] for c in result['candidates']}, set(self.local_bases))
        self.assertNotIn(file_token, json.dumps(result, ensure_ascii=False))
        for candidate in result['candidates']:
            self.assertIn('本机', candidate['source'])
            self.assertEqual(plugin._connection_scan_pending[candidate['id']][1], file_token)
        plugin.config.save_config.assert_not_called()

    async def test_file_token_without_current_token_still_never_probes_remote(self):
        plugin = Plugin(token='')
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / 'config.json'
            token_path.write_text(json.dumps({'token': 'local-only-secret'}), encoding='utf-8')
            with patch.object(scan, 'TOKEN_PATHS', (token_path,)):
                plugin._probe_connection.return_value = True
                result = await plugin.scan_connections()
        self.assertEqual([call.args for call in plugin._probe_connection.await_args_list],
                         [(base, 'local-only-secret') for base in self.local_bases])
        self.assertEqual(len(result['candidates']), len(self.local_bases))

    async def test_no_current_token_and_no_token_files_returns_empty_results(self):
        plugin = Plugin(token='')
        result = await plugin.scan_connections()
        self.assertTrue(result['experimental'])
        self.assertEqual(result['candidates'], [])
        self.assertTrue(result['notice'])
        self.assertEqual(plugin._connection_scan_pending, {})
        plugin._probe_connection.assert_not_awaited()
        plugin.config.save_config.assert_not_called()

    async def test_save_failure_rolls_back_config_and_keeps_candidate_for_retry(self):
        plugin = Plugin()
        candidate = await self.discover_one(plugin)
        before = dict(plugin.config)
        config_identity = plugin.config
        plugin.config.save_config.side_effect = RuntimeError('disk unavailable')
        with self.assertRaisesRegex(RuntimeError, 'disk unavailable'):
            await plugin.apply_connection_scan(candidate['id'])
        self.assertIs(plugin.config, config_identity)
        self.assertEqual(dict(plugin.config), before)
        plugin.config.save_config.assert_called_once_with()
        self.assertIn(candidate['id'], plugin._connection_scan_pending)
        plugin.config.save_config.side_effect = None
        result = await plugin.apply_connection_scan(candidate['id'])
        self.assertTrue(result['saved'])
        self.assertEqual(plugin.config.save_config.call_count, 2)

    async def test_missing_persistence_rejects_without_mutating_config(self):
        plugin = Plugin()
        candidate = await self.discover_one(plugin)
        before = dict(plugin.config)
        plugin.config.save_config = None
        with self.assertRaisesRegex(ValueError, 'config persistence unavailable'):
            await plugin.apply_connection_scan(candidate['id'])
        self.assertEqual(dict(plugin.config), before)


if __name__ == '__main__':
    unittest.main()
