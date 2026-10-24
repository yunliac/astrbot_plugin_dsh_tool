"""Isolated progress observer tests: no AstrBot, plugin main, network or cloud."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'progress_reporting_isolated', ROOT / 'progress_reporting.py')
progress = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(progress)


class Plugin(progress.ProgressReportingMixin):
    def __init__(self, config=None, origin='chat-a', channel='lane-a'):
        self.config = dict(config or {})
        self._closing = False
        self.origin = origin
        self.payload = {'umo': f'tool:{origin}#{channel}'}
        self.managed = {'ok': True, 'sessions': [
            {'umo': self.payload['umo'], 'sessionId': 'sid-exact'},
        ]}
        self.evidence = {'ok': True, 'status': 'running',
                         'logs': 'CURRENT_EVIDENCE'}
        self.context = SimpleNamespace(
            get_current_chat_provider_id=Mock(return_value='provider-current'),
            llm_generate=AsyncMock(return_value=SimpleNamespace(
                completion_text='已完成检查，正在运行测试。')),
        )
        self._management_request = AsyncMock(side_effect=self._read)
        self._send_to_chat = AsyncMock()

    async def _read(self, path, params=None):
        if path == '/sessions/managed':
            return self.managed, 'private-management-base'
        if path == '/sessions/runtime':
            return self.evidence, 'private-management-base'
        raise AssertionError(f'Unexpected non-read endpoint: {path}')


class ProgressReportingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Replace only the tested module's asyncio reference, not global sleep.
        # wait_for still runs the real cancellation/timeout implementation.
        self.clock = SimpleNamespace(
            sleep=AsyncMock(),
            wait_for=AsyncMock(side_effect=asyncio.wait_for),
            CancelledError=asyncio.CancelledError,
        )
        self.asyncio_patch = patch.object(progress, 'asyncio', self.clock)
        self.asyncio_patch.start()
        self.addCleanup(self.asyncio_patch.stop)

    def assert_no_external_calls(self, plugin):
        plugin._management_request.assert_not_awaited()
        plugin.context.get_current_chat_provider_id.assert_not_called()
        plugin.context.llm_generate.assert_not_awaited()
        plugin._send_to_chat.assert_not_awaited()

    async def test_disabled_does_not_sleep_read_generate_or_push(self):
        plugin = Plugin({'auto_progress_enabled': False})
        await plugin._watch_progress(plugin.payload)
        self.clock.sleep.assert_not_awaited()
        self.assert_no_external_calls(plugin)

    async def test_exact_binding_provider_origin_tool_free_and_bounded(self):
        plugin = Plugin({'auto_progress_max_reports': 1}, origin='chat-a#topic')
        plugin.managed['sessions'] = [
            {'umo': 'tool:chat-b#lane-a', 'sessionId': 'FOREIGN_SECRET'},
            {'umo': 'tool:chat-a#topic#lane-a-extra', 'sessionId': 'PREFIX_SECRET'},
            {'umo': plugin.payload['umo'], 'sessionId': 'sid-exact'},
        ]
        plugin.evidence['logs'] = 'CURRENT_EVIDENCE' + 'x' * 24000
        plugin.context.llm_generate.return_value.completion_text = '总结' * 3000
        await plugin._watch_progress(plugin.payload)

        calls = plugin._management_request.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args, ('/sessions/managed',))
        self.assertEqual(calls[1].args, ('/sessions/runtime', {
            'umo': plugin.payload['umo'], 'sessionId': 'sid-exact', 'detail': '1'}))
        plugin.context.get_current_chat_provider_id.assert_called_once_with('chat-a#topic')
        plugin.context.llm_generate.assert_awaited_once()
        kwargs = plugin.context.llm_generate.await_args.kwargs
        self.assertEqual(kwargs['chat_provider_id'], 'provider-current')
        self.assertIn('tools', kwargs)
        self.assertIsNone(kwargs['tools'])
        self.assertIn('CURRENT_EVIDENCE', kwargs['prompt'])
        self.assertLessEqual(len(kwargs['prompt']), 16500)
        for secret in ('FOREIGN_SECRET', 'PREFIX_SECRET', 'private-management-base'):
            self.assertNotIn(secret, json.dumps(kwargs, ensure_ascii=False))
        self.assertIn('不可信', kwargs['system_prompt'])
        self.assertIn('不猜百分比', kwargs['system_prompt'])
        self.assertEqual(self.clock.wait_for.await_args.kwargs['timeout'], 45)
        plugin._send_to_chat.assert_awaited_once()
        origin, text = plugin._send_to_chat.await_args.args
        self.assertEqual(origin, 'chat-a#topic')
        self.assertIn('lane-a', text)
        self.assertLessEqual(len(text), 2200)

    async def test_foreign_and_prefix_only_bindings_are_never_observed(self):
        plugin = Plugin({'auto_progress_max_reports': 2})
        plugin.managed['sessions'] = [
            {'umo': 'tool:chat-b#lane-a', 'sessionId': 'foreign'},
            {'umo': plugin.payload['umo'] + '-extra', 'sessionId': 'prefix'},
            {'umo': 'tool:chat-a#lane-b', 'sessionId': 'other-lane'},
        ]
        await plugin._watch_progress(plugin.payload)
        self.assertEqual(plugin._management_request.await_count, 2)
        self.assertTrue(all(call.args == ('/sessions/managed',)
                            for call in plugin._management_request.await_args_list))
        plugin.context.get_current_chat_provider_id.assert_not_called()
        plugin.context.llm_generate.assert_not_awaited()
        plugin._send_to_chat.assert_not_awaited()
        self.assertEqual([call.args[0] for call in self.clock.sleep.await_args_list], [120, 180])

    async def test_duplicate_exact_binding_is_not_guessed(self):
        plugin = Plugin({'auto_progress_max_reports': 1})
        plugin.managed['sessions'].append({
            'umo': plugin.payload['umo'], 'sessionId': 'second-session'})
        await plugin._watch_progress(plugin.payload)
        plugin._management_request.assert_awaited_once_with('/sessions/managed')
        plugin.context.llm_generate.assert_not_awaited()
        plugin._send_to_chat.assert_not_awaited()

    async def test_cancellation_during_delay_propagates_without_push(self):
        plugin = Plugin()
        self.clock.sleep.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await plugin._watch_progress(plugin.payload)
        self.assert_no_external_calls(plugin)

    async def test_cancellation_during_llm_propagates_without_fallback_push(self):
        plugin = Plugin({'auto_progress_max_reports': 1})
        plugin.context.llm_generate.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await plugin._watch_progress(plugin.payload)
        plugin._send_to_chat.assert_not_awaited()

    async def test_llm_error_sends_sanitized_status_fallback(self):
        plugin = Plugin({'auto_progress_max_reports': 1})
        plugin.context.llm_generate.side_effect = RuntimeError(
            'SECRET_TOKEN https://private-provider traceback')
        await plugin._watch_progress(plugin.payload)
        plugin._send_to_chat.assert_awaited_once()
        origin, text = plugin._send_to_chat.await_args.args
        self.assertEqual(origin, plugin.origin)
        self.assertIn('running', text)
        self.assertIn('尚未返回最终结果', text)
        self.assertNotIn('SECRET_TOKEN', text)
        self.assertNotIn('private-provider', text)
        self.assertNotIn('traceback', text)

    async def test_blank_llm_result_falls_back_to_evidence_status(self):
        plugin = Plugin({'auto_progress_max_reports': 1})
        plugin.context.llm_generate.return_value.completion_text = '  '
        await plugin._watch_progress(plugin.payload)
        self.assertIn('running', plugin._send_to_chat.await_args.args[1])

    async def test_default_and_capped_max_reports_and_minimum_intervals(self):
        for config, expected, delay, interval in [
            ({'auto_progress_max_reports': 2}, 2, 120, 180),
            ({'auto_progress_max_reports': 999,
              'auto_progress_delay_sec': 0,
              'auto_progress_interval_sec': -1}, 20, 30, 30),
        ]:
            with self.subTest(config=config):
                self.clock.sleep.reset_mock()
                plugin = Plugin(config)
                await plugin._watch_progress(plugin.payload)
                self.assertEqual(plugin._send_to_chat.await_count, expected)
                self.assertEqual(plugin.context.llm_generate.await_count, expected)
                self.assertEqual(plugin._management_request.await_count, 2 * expected)
                self.assertEqual([call.args[0] for call in self.clock.sleep.await_args_list],
                                 [delay] + [interval] * (expected - 1))

    async def test_zero_and_default_unlimited_until_cancelled(self):
        for config in ({}, {'auto_progress_max_reports': 0}):
            plugin = Plugin(config)
            async def stop_after_seven(*args):
                if plugin._send_to_chat.await_count >= 7:
                    raise asyncio.CancelledError
            plugin._send_to_chat.side_effect = stop_after_seven
            with self.assertRaises(asyncio.CancelledError):
                await plugin._watch_progress(plugin.payload)
            self.assertEqual(plugin._send_to_chat.await_count, 7)

    async def test_shutdown_while_llm_running_prevents_late_push(self):
        plugin = Plugin({'auto_progress_max_reports': 1})

        async def finish_after_shutdown(**kwargs):
            plugin._closing = True
            return SimpleNamespace(completion_text='late progress')

        plugin.context.llm_generate.side_effect = finish_after_shutdown
        await plugin._watch_progress(plugin.payload)
        plugin._send_to_chat.assert_not_awaited()

    async def test_runtime_outage_never_generates_or_pushes(self):
        plugin = Plugin({'auto_progress_max_reports': 1})
        plugin.evidence = {'ok': False, 'error': 'SECRET_TOKEN'}
        await plugin._watch_progress(plugin.payload)
        plugin.context.llm_generate.assert_not_awaited()
        plugin._send_to_chat.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
