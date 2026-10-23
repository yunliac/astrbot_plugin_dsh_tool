"""发布前冒烟测试：用假 astrbot 模块 + 本地 SSE 服务器验证 dsh_task 核心逻辑。"""

import asyncio
import importlib.util
import json
import inspect
from unittest.mock import patch
import logging
import shutil
import socket
import sys
import threading
import traceback
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
# 伪造 astrbot 模块
# --------------------------------------------------------------------------- #
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.AstrBotConfig = dict
api.logger = logging.getLogger("test")
astrbot.api = api
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api


class _Filter:
    def on_llm_request(self, **kwargs):
        return lambda fn: fn

    def llm_tool(self, name=None, **kwargs):
        def decorator(fn):
            fn.__llm_tool_name__ = name
            return fn

        return decorator


class MessageChain:
    def __init__(self, chain=None, **kwargs):
        self.chain = list(chain or [])


events = types.ModuleType("astrbot.api.event")
events.AstrMessageEvent = object
events.MessageChain = MessageChain
events.filter = _Filter()
sys.modules["astrbot.api.event"] = events


class Plain:
    def __init__(self, text=""):
        self.text = text


components = types.ModuleType("astrbot.api.message_components")
components.Plain = Plain
sys.modules["astrbot.api.message_components"] = components

star_mod = types.ModuleType("astrbot.api.star")


class Context:
    pass


class Star:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config or {}
        self.logger = logging.getLogger("test")


class StarTools:
    sent: list[tuple[str, str]] = []

    @classmethod
    async def send_message(cls, session, message_chain):
        text = "".join(getattr(c, "text", str(c)) for c in message_chain.chain)
        cls.sent.append((session, text))
        return True


def register(*args, **kwargs):
    def decorator(cls):
        cls.__registered__ = args
        return cls

    return decorator


star_mod.Context = Context
star_mod.Star = Star
star_mod.StarTools = StarTools
star_mod.register = register
sys.modules["astrbot.api.star"] = star_mod

core = types.ModuleType("astrbot.core")
utils = types.ModuleType("astrbot.core.utils")
paths = types.ModuleType("astrbot.core.utils.astrbot_path")
paths.get_astrbot_config_path = lambda: "/nonexistent"
sys.modules["astrbot.core"] = core
sys.modules["astrbot.core.utils"] = utils
sys.modules["astrbot.core.utils.astrbot_path"] = paths

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
spec = importlib.util.spec_from_file_location("dsh_tool_main", MODULE_PATH)
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)


# --------------------------------------------------------------------------- #
# SSE 测试服务器
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        self.server.captured = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(raw.decode("utf-8")) if raw else {},
        }
        self.send_response(self.server.status)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in self.server.chunks:
            self.wfile.write(chunk)
            self.wfile.flush()

    def log_message(self, *args):
        pass


def make_server(chunks, status=200):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.chunks = chunks
    server.status = status
    server.captured = None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def run(coro):
    return asyncio.run(coro)


def close(server):
    server.shutdown()
    server.server_close()


# --------------------------------------------------------------------------- #
# 测试
# --------------------------------------------------------------------------- #
def test_basic_stream():
    server = make_server(
        [
            sse("ack", {"sessionId": "session-abcdef123456"}),
            sse("text", {"text": "第一段"}),
            sse("text", {"text": "第二段"}),
            sse("done", {"sessionId": "session-abcdef123456"}),
        ]
    )
    base = f"http://127.0.0.1:{server.server_port}"
    text, error, session_id = run(
        main._call_ingress(base, "secret", {"text": "x"}, 10, 6000)
    )
    close(server)
    assert text == "第一段\n第二段", text
    assert error is None, error
    assert session_id == "session-abcdef123456"
    assert server.captured["path"] == "/inbound"
    assert server.captured["headers"]["Authorization"] == "Bearer secret"
    assert server.captured["body"]["text"] == "x"


def test_multiline_data_and_no_blank_line():
    body = (
        b"event: text\n"
        b'data: {"text":\n'
        b'data: "\xe5\xa4\x9a\xe8\xa1\x8c"}\n'
        b"event: done\ndata: {}\n"
    )
    server = make_server([body])
    base = f"http://127.0.0.1:{server.server_port}"
    text, error, session_id = run(main._call_ingress(base, "", {}, 10, 6000))
    close(server)
    assert text == "多行", text
    assert error is None
    assert session_id == ""
    assert "Authorization" not in {k for k in server.captured["headers"]}


def test_error_event():
    server = make_server([sse("error", {"message": "boom"}), sse("done", {})])
    base = f"http://127.0.0.1:{server.server_port}"
    text, error, _ = run(main._call_ingress(base, "t", {}, 10, 6000))
    close(server)
    assert text == "", repr(text)
    assert error == "boom", error


def test_http_error():
    server = make_server([], status=401)
    base = f"http://127.0.0.1:{server.server_port}"
    text, error, session_id = run(main._call_ingress(base, "bad", {}, 10, 6000))
    close(server)
    assert text == ""
    assert session_id == ""
    assert error.startswith("HTTP 401"), error
    assert "token" in error


def test_clean_and_truncate():
    assert main._clean("结果—— 本回合结束", 6000) == "结果"
    long_text = "x" * 100
    cleaned = main._clean(long_text, 10)
    assert cleaned.startswith("x" * 10)
    assert "截断" in cleaned


def test_normalize_base():
    assert main._normalize_base("127.0.0.1:3188/") == "http://127.0.0.1:3188"
    assert main._normalize_base("https://dsh.example.com/") == "https://dsh.example.com"
    assert main._normalize_base("") == ""


def test_lane_umo():
    base = "tool:aiocqhttp:GroupMessage:123"
    assert main._lane_umo(base, "") == (base, "")
    assert main._lane_umo(base, "auto") == (base, "")
    assert main._lane_umo(base, "build") == (f"{base}#build", "build")
    assert main._lane_umo(base, "a b/c") == (f"{base}#a-b-c", "a-b-c")
    assert main._lane_umo(base, "分析") == (f"{base}#分析", "分析")

    umo_a, lane_a = main._lane_umo(base, "new")
    umo_b, lane_b = main._lane_umo(base, "new")
    assert lane_a == lane_b == "new"
    assert umo_a.startswith(f"{base}#new-")
    assert umo_a != umo_b


def test_chunk_text():
    chunks = main._chunk_text("a" * 4000)
    assert "".join(chunks) == "a" * 4000
    assert all(len(chunk) <= main.CHUNK_SIZE for chunk in chunks)


def test_bridge_config_and_settings():
    tmp = Path(__file__).parent / "_tmp_bridge"
    tmp.mkdir(exist_ok=True)
    try:
        path = tmp / "astrbot_plugin_dsh_config.json"
        path.write_text(
            json.dumps(
                {
                    "ingress_url": "bridge:3188",
                    "token": "bridge-token",
                    "timeout_sec": 300,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8-sig",
        )
        loaded = main._load_bridge_config(str(path))
        assert loaded["token"] == "bridge-token"
        assert loaded["ingress_url"] == "bridge:3188"

        plugin = main.DshToolPlugin(None, {"bridge_config_path": str(path)})
        bases, token, timeout, max_chars = plugin._resolve_settings()
        assert bases == ["http://bridge:3188"], bases
        assert token == "bridge-token"
        assert timeout == 300
        assert max_chars == 6000
        assert plugin._max_parallel() == 3

        plugin = main.DshToolPlugin(
            None,
            {
                "bridge_config_path": str(path),
                "ingress_url": "http://127.0.0.1:9999",
                "token": "own-token",
                "timeout_sec": 5,
                "max_result_chars": 100000,
                "max_parallel": 9,
            },
        )
        bases, token, timeout, max_chars = plugin._resolve_settings()
        assert bases == ["http://127.0.0.1:9999"]
        assert token == "own-token"
        assert timeout == 30, timeout
        assert max_chars == 20000, max_chars
        assert plugin._max_parallel() == 9
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class FakeEvent:
    unified_msg_origin = "test:group:123"

    def __init__(self, admin=True, sender="42", group="123"):
        self.admin = admin
        self.sender = sender
        self.group = group

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def is_admin(self):
        return self.admin


def test_permission():
    plugin = main.DshToolPlugin(None, {})
    assert plugin._check_permission(FakeEvent(admin=True)) == (True, "")
    allowed, reason = plugin._check_permission(FakeEvent(admin=False))
    assert not allowed and reason

    plugin = main.DshToolPlugin(None, {"admin_only": False})
    assert plugin._check_permission(FakeEvent(admin=False)) == (True, "")

    plugin = main.DshToolPlugin(None, {"allow_users": ["42"]})
    assert plugin._check_permission(FakeEvent(admin=False, sender="42")) == (True, "")


def test_dispatch_fallback_and_tool_end_to_end():
    server = make_server([sse("text", {"text": "完成"}), sse("done", {})])
    base = f"http://127.0.0.1:{server.server_port}"
    plugin = main.DshToolPlugin(None, {"ingress_url": base, "token": "tk"})
    result = run(plugin.dsh_task(FakeEvent(), "看一下磁盘"))
    assert result == "完成", result
    assert server.captured["body"]["umo"] == "tool:test:group:123"
    assert server.captured["body"]["chatType"] == "group"
    assert server.captured["body"]["groupId"] == "123"
    close(server)

    # 第一个地址连不上时，应自动落到下一个候选
    server2 = make_server([sse("text", {"text": "完成"}), sse("done", {})])
    base2 = f"http://127.0.0.1:{server2.server_port}"
    dead = f"http://127.0.0.1:{free_port()}"
    plugin = main.DshToolPlugin(None, {})
    result, session_id = run(plugin._dispatch([dead, base2], "", {}, 10, 6000))
    assert result == "完成", result
    assert session_id == ""
    close(server2)


def test_named_lane_sync_footer():
    server = make_server(
        [
            sse("ack", {"sessionId": "session-abcdef123456"}),
            sse("text", {"text": "结果"}),
            sse("done", {"sessionId": "session-abcdef123456"}),
        ]
    )
    base = f"http://127.0.0.1:{server.server_port}"
    plugin = main.DshToolPlugin(None, {"ingress_url": base})
    result = run(plugin.dsh_task(FakeEvent(), "任务", session="build"))
    assert result.startswith("结果"), result
    assert "通道 build" in result
    assert "abcdef12" in result
    assert server.captured["body"]["umo"].endswith("#build")
    close(server)

    # 不同通道 → 不同 umo
    server2 = make_server([sse("text", {"text": "ok"}), sse("done", {})])
    base2 = f"http://127.0.0.1:{server2.server_port}"
    plugin = main.DshToolPlugin(None, {"ingress_url": base2})
    run(plugin.dsh_task(FakeEvent(), "任务", session="research"))
    assert server2.captured["body"]["umo"].endswith("#research")
    close(server2)


def test_background_delivery():
    async def scenario():
        StarTools.sent.clear()
        server = make_server(
            [
                sse("ack", {"sessionId": "session-112233445566"}),
                sse("text", {"text": "后台完成"}),
                sse("done", {"sessionId": "session-112233445566"}),
            ]
        )
        base = f"http://127.0.0.1:{server.server_port}"
        plugin = main.DshToolPlugin(None, {"ingress_url": base, "max_parallel": 2})
        result = await plugin.dsh_task(
            FakeEvent(), "后台任务", session="build", background=True
        )
        assert "已提交" in result and "#" in result, result
        assert "通道 build" in result

        for _ in range(100):
            if not plugin._bg_tasks:
                break
            await asyncio.gather(*list(plugin._bg_tasks), return_exceptions=True)

        close(server)
        assert StarTools.sent, "后台结果没有发送"
        umo, text = StarTools.sent[0]
        assert umo == "test:group:123", umo
        assert "【DSH 后台任务完成】" in text, text
        assert "11223344" in text, text
        assert "后台完成" in text, text
        assert server.captured["body"]["umo"].endswith("#build")
        assert not plugin._bg_lanes, plugin._bg_lanes

    run(scenario())


def test_lane_busy_rejected():
    async def scenario():
        plugin = main.DshToolPlugin(None, {"ingress_url": "http://127.0.0.1:1"})
        busy = "tool:test:group:123#build"
        plugin._bg_lanes.add(busy)
        try:
            bg = await plugin.dsh_task(
                FakeEvent(), "任务", session="build", background=True
            )
            assert "已有后台任务" in bg, bg
            sync = await plugin.dsh_task(FakeEvent(), "任务", session="build")
            assert "已有后台任务" in sync, sync
        finally:
            plugin._bg_lanes.discard(busy)

    run(scenario())


def test_background_limit():
    async def scenario():
        plugin = main.DshToolPlugin(
            None, {"ingress_url": "http://127.0.0.1:1", "max_parallel": 1}
        )
        dummy = asyncio.create_task(asyncio.sleep(30))
        plugin._bg_tasks.add(dummy)
        try:
            result = await plugin.dsh_task(FakeEvent(), "任务", background=True)
            assert "上限" in result, result
        finally:
            dummy.cancel()
            await asyncio.gather(dummy, return_exceptions=True)

    run(scenario())


def test_bool_coercion():
    assert main._as_bool(True) is True
    assert main._as_bool("true") is True
    assert main._as_bool("false") is False
    assert main._as_bool(None, False) is False




def test_idle_reclaim_safety_and_success():
    async def scenario():
        plugin = main.DshToolPlugin(None, {
            "ingress_url": "http://test", "token": "test-token",
            "idle_reclaim_enabled": True, "idle_timeout_sec": 60})
        lane = "tool:test#work"
        def record(age=61):
            return {"base": "http://test", "session_id": "session-test",
                    "last_used": main.time.monotonic() - age}
        calls = []
        status = 200
        reply = {"ok": True, "released": True, "sessionId": "session-test"}
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, url, **kwargs):
                calls.append((url, kwargs))
                return main.httpx.Response(status, json=reply,
                    request=main.httpx.Request("POST", url))
        with patch.object(main.httpx, "AsyncClient", Client):
            plugin._session_activity[lane] = record(5)
            await plugin._reclaim_idle_sessions()
            assert not calls, "recently used sessions must not be reclaimed"
            plugin._session_activity[lane] = record()
            plugin._active_lanes[lane] = 1
            await plugin._reclaim_idle_sessions()
            assert not calls, "foreground/queued calls must be protected"
            plugin._active_lanes.clear()
            plugin._bg_lanes.add(lane)
            await plugin._reclaim_idle_sessions()
            assert not calls, "queued background calls must be protected"
            plugin._bg_lanes.clear()
            plugin.config["idle_reclaim_enabled"] = False
            await plugin._reclaim_idle_sessions()
            assert not calls
            plugin.config["idle_reclaim_enabled"] = True
            status = 409
            await plugin._reclaim_idle_sessions()
            assert lane in plugin._session_activity
            status = 200
            reply = []
            await plugin._reclaim_idle_sessions()
            assert lane in plugin._session_activity, "malformed replies must not drop records"
            reply = {"ok": True, "released": True, "sessionId": "session-other"}
            await plugin._reclaim_idle_sessions()
            assert lane in plugin._session_activity, "response identity must match"
            reply = {"ok": True, "released": False, "sessionId": "session-test"}
            await plugin._reclaim_idle_sessions()
            assert lane in plugin._session_activity
            reply["released"] = True
            await plugin._reclaim_idle_sessions()
            assert lane not in plugin._session_activity
            url, kwargs = calls[-1]
            assert url == "http://test/session/release"
            assert kwargs["headers"]["Authorization"] == "Bearer test-token"
            assert kwargs["json"] == {"umo": lane, "sessionId": "session-test", "idle_seconds": 60}
        await plugin.terminate()
    run(scenario())


def test_idle_reclaim_unsupported_warn_once_and_retry_errors():
    async def scenario():
        plugin = main.DshToolPlugin(None, {
            "ingress_url": "http://test", "idle_reclaim_enabled": True,
            "idle_timeout_sec": 60})
        lane = "tool:test"
        plugin._session_activity[lane] = {"base": "http://test",
            "session_id": "session-test", "last_used": main.time.monotonic() - 61}
        calls = []
        status = 500
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, url, **kwargs):
                calls.append(url)
                return main.httpx.Response(status, json={"ok": False},
                    request=main.httpx.Request("POST", url))
        with patch.object(main.httpx, "AsyncClient", Client):
            await plugin._reclaim_idle_sessions()
            assert lane in plugin._session_activity and len(calls) == 1
            status = 404
            await plugin._reclaim_idle_sessions()
            await plugin._reclaim_idle_sessions()
            assert len(calls) == 2, "unsupported endpoints should not be polled repeatedly"
            assert lane in plugin._session_activity
        await plugin.terminate()
    run(scenario())


def test_idle_reaper_lifecycle_and_config_bounds():
    async def scenario():
        plugin = main.DshToolPlugin(None, {})
        await plugin.initialize()
        assert plugin._reaper_task is None
        plugin.config["idle_reclaim_enabled"] = "true"
        plugin.config["idle_timeout_sec"] = 1
        plugin.config["idle_check_interval_sec"] = 1
        assert plugin._idle_timeout() == 60
        assert plugin._reclaim_interval() == 10
        await plugin.initialize()
        task = plugin._reaper_task
        assert task is not None
        await plugin.initialize()
        assert plugin._reaper_task is task
        await plugin.terminate()
        assert task.done()
    run(scenario())


def test_activity_tracking_and_same_lane_serialization():
    async def scenario():
        plugin = main.DshToolPlugin(None, {})
        started = asyncio.Event()
        finish = asyncio.Event()
        calls = []
        async def fake_ingress(base, token, payload, timeout, max_chars):
            calls.append(payload["umo"])
            started.set()
            await finish.wait()
            return "done", None, "session-track"
        lane = "tool:test"
        with patch.object(main, "_call_ingress", fake_ingress):
            first = asyncio.create_task(plugin._dispatch(["http://test"], "", {"umo": lane}, 60, 500))
            await started.wait()
            second = asyncio.create_task(plugin._dispatch(["http://test"], "", {"umo": lane}, 60, 500))
            await asyncio.sleep(0)
            assert len(calls) == 1 and plugin._active_lanes[lane] == 2
            finish.set()
            await asyncio.gather(first, second)
        assert len(calls) == 2 and not plugin._active_lanes
        record = plugin._session_activity[lane]
        assert record["session_id"] == "session-track"
        assert main.time.monotonic() - record["last_used"] < 1
        assert "token" not in record
        await plugin.terminate()
    run(scenario())


def test_reclaim_serializes_new_request_and_preserves_new_record():
    async def scenario():
        plugin = main.DshToolPlugin(None, {
            "ingress_url": "http://test", "idle_reclaim_enabled": True,
            "idle_timeout_sec": 60})
        lane = "tool:test"
        plugin._session_activity[lane] = {"base": "http://test",
            "session_id": "old", "last_used": main.time.monotonic() - 61}
        releasing, finish = asyncio.Event(), asyncio.Event()
        calls = []
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, url, **kwargs):
                releasing.set()
                await finish.wait()
                return main.httpx.Response(200,
                    json={"ok": True, "released": True, "sessionId": "old"},
                    request=main.httpx.Request("POST", url))
        async def ingress(*args):
            calls.append(True)
            return "restored", None, "new"
        with patch.object(main.httpx, "AsyncClient", Client), patch.object(main, "_call_ingress", ingress):
            collector = asyncio.create_task(plugin._reclaim_idle_sessions())
            await releasing.wait()
            request = asyncio.create_task(plugin._dispatch(["http://test"], "", {"umo": lane}, 60, 500))
            await asyncio.sleep(0)
            assert not calls
            finish.set()
            await asyncio.gather(collector, request)
        assert plugin._session_activity[lane]["session_id"] == "new"
        assert not plugin._active_lanes
        await plugin.terminate()
    run(scenario())


def test_capability_hint_respects_tool_selection_and_permission():
    async def scenario():
        plugin = main.DshToolPlugin(None, {})
        class Tools:
            def __init__(self, active=True):
                self.tool = types.SimpleNamespace(active=active)
            def get_tool(self, name): return self.tool
        request = types.SimpleNamespace(func_tool=Tools(), system_prompt="persona")
        await plugin.remind_dsh_capability(FakeEvent(), request)
        assert "当前 DSH 工具能力" in request.system_prompt
        assert request.system_prompt.startswith("persona")
        request = types.SimpleNamespace(func_tool=None, system_prompt="persona")
        await plugin.remind_dsh_capability(FakeEvent(), request)
        assert request.system_prompt == "persona"
        for event, tools in [(FakeEvent(admin=False), Tools()), (FakeEvent(), Tools(False))]:
            request = types.SimpleNamespace(func_tool=tools, system_prompt="persona")
            await plugin.remind_dsh_capability(event, request)
            assert request.system_prompt == "persona"
    run(scenario())


def test_session_flags_export_and_reclaim_exemption():
    async def scenario():
        plugin = main.DshToolPlugin(None, {
            "ingress_url": "http://127.0.0.1:9", "idle_reclaim_enabled": True,
            "idle_timeout_sec": 60})
        umo = "tool:test:FriendMessage:1#deploy"
        record = plugin._record_session(
            umo, "session-deploy", "http://127.0.0.1:9", status="idle")
        assert record["long_term"] is False and record["exempt"] is False
        assert "token" not in record and "base" in record

        updated = await plugin.update_managed_session(record["id"], long_term=True)
        assert updated["long_term"] is True and updated["exempt"] is False
        # long_term is only a durable label; it must not disable reclaim.
        assert plugin.session_store.is_exempt(umo, "session-deploy") is False

        await plugin.update_managed_session(record["id"], exempt=True)
        assert plugin.session_store.is_exempt(umo, "session-deploy") is True
        plugin.session_store.append_exchange(record["id"], "检查磁盘", "剩余 10G")

        plugin._session_activity[umo] = {
            "base": "http://127.0.0.1:9", "session_id": "session-deploy",
            "last_used": main.time.monotonic() - 61}
        calls = []
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, url, **kwargs):
                calls.append(url)
                return main.httpx.Response(200, json={"ok": True, "released": True,
                    "sessionId": "session-deploy"}, request=main.httpx.Request("POST", url))
        with patch.object(main.httpx, "AsyncClient", Client):
            await plugin._reclaim_idle_sessions()
            assert not calls, "exempt sessions must not be reclaimed"

            await plugin.update_managed_session(record["id"], exempt=False)
            await plugin._reclaim_idle_sessions()
            assert calls and calls[-1].endswith("/session/release")
            assert plugin.session_store.get(record["id"])["status"] == "cold"

        exported = await plugin.export_managed_session(record["id"], "markdown")
        assert "检查磁盘" in exported["content"] and "剩余 10G" in exported["content"]
        exported_json = await plugin.export_managed_session(record["id"], "json")
        payload = json.loads(exported_json["content"])
        assert payload["messages"][0]["role"] == "user"
        listing = await plugin.list_managed_sessions()
        row = next(item for item in listing["sessions"] if item["id"] == record["id"])
        assert "base" not in row and "umo" in row
        assert listing["idle"]["enabled"] is True
        await plugin.terminate()
    run(scenario())


def test_management_tool_rejects_ambiguous_targets():
    async def scenario():
        plugin = main.DshToolPlugin(None, {"ingress_url": "http://127.0.0.1:9"})
        event = FakeEvent()
        event.unified_msg_origin = "test:group:123"
        for channel in ("a", "b"):
            plugin._record_session(f"tool:test:group:123#{channel}", f"session-{channel}",
                                   "http://127.0.0.1:9", status="idle")
        result = await plugin.dsh_session(event, "update", long_term=True)
        assert "不明确" in result
        assert all(not row["long_term"] for row in plugin.session_store.list())
        result = await plugin.dsh_session(event, "update", session="a", exempt=True)
        assert '"exempt": true' in result
        assert plugin.session_store.is_exempt("tool:test:group:123#a")
        result = await plugin.dsh_session(event, "list")
        assert "session-a" in result or '"a"' in result
        await plugin.terminate()
    run(scenario())


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                if inspect.iscoroutinefunction(func):
                    run(func())
                else:
                    func()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc!r}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
