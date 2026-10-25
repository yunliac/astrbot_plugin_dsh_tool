"""Authenticated AstrBot 4.29 plugin-view adapter (no separate HTTP server).

AstrBot discovers ``views/dsh/index.html`` automatically. All data routes are
registered through Context, so AstrBot's dashboard/plugin-scope auth runs first.
The iframe uses the injected AstrBotPluginView bridge; no token reaches the page.
"""
from __future__ import annotations

import asyncio
from typing import Any

PLUGIN_NAME = "astrbot_plugin_dsh_tool"
_FIELDS = frozenset({
    "id", "umo", "session_id", "origin", "channel", "title", "cwd", "status",
    "last_used", "long_term", "exempt", "remote_seen_at", "idle_remaining_sec",
})


def _record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid session record")
    return {key: value[key] for key in _FIELDS if key in value}


def _listing(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("sessions"), list):
        raise ValueError("invalid session list")
    idle = value.get("idle") or {}
    return {
        "sessions": [_record(item) for item in value["sessions"]],
        "ingress_available": value.get("ingress_available") is True,
        "idle": {"enabled": idle.get("enabled") is True,
                 "timeout_sec": idle.get("timeout_sec", 0)},
        # Upstream exception messages may contain credentials or ingress URLs.
        "message": "" if value.get("ingress_available") is True else
                   "DSH 暂不可达：显示本地保留记录，运行状态可能已过期。",
    }


def _sid(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("invalid record id")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("invalid record id")
    return value


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "message": message, "data": {}}


def register_webui(plugin: Any) -> bool:
    """Register protected routes; return False on unsupported AstrBot versions.

    Call once from initialize(). Repeated calls on the same instance are safe.
    There is deliberately no unauthenticated fallback or extra listening port.
    """
    if getattr(plugin, "_dsh_webui_routes", None):
        return True
    register = getattr(plugin.context, "register_web_api", None)
    if not callable(register):
        return False
    try:
        from astrbot.api.web import request
    except ImportError:
        return False

    async def invoke(operation):
        # Defense in depth; outer AstrBot router is responsible for actual auth.
        if not getattr(request, "username", None):
            return _error("需要 AstrBot 管理员登录。")
        try:
            return {"status": "ok", "data": await operation()}
        except (KeyError, LookupError):
            return _error("会话不存在或已被删除，请刷新列表。")
        except (TypeError, ValueError):
            return _error("请求参数或会话数据不合法。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            diagnose = getattr(plugin, '_diag', None)
            if callable(diagnose):
                diagnose('error', '管理页面操作失败：' + type(exc).__name__)
            # Do not return str(exc): transport errors can embed bearer tokens.
            return _error("操作失败，请检查 DSH 连接后重试。")

    async def sessions():
        async def operation():
            return _listing(await plugin.list_managed_sessions())
        return await invoke(operation)

    async def refresh():
        async def operation():
            return _listing(await plugin.refresh_managed_sessions())
        return await invoke(operation)

    async def update():
        async def operation():
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"id", "long_term", "exempt", "title"}:
                raise ValueError("invalid body")
            sid = _sid(body.get("id"))
            changes = {key: body[key] for key in ("long_term", "exempt", "title") if key in body}
            if not changes:
                raise ValueError("empty update")
            for key in ("long_term", "exempt"):
                if key in changes and not isinstance(changes[key], bool):
                    raise ValueError("expected boolean")
            if "title" in changes and (not isinstance(changes["title"], str) or len(changes["title"]) > 200):
                raise ValueError("invalid title")
            return _record(await plugin.update_managed_session(sid, **changes))
        return await invoke(operation)

    async def export():
        async def operation():
            sid = _sid(request.query.get("id"))
            fmt = request.query.get("format", "markdown")
            if fmt not in {"markdown", "json"}:
                raise ValueError("invalid export format")
            result = await plugin.export_managed_session(sid, fmt)
            if not isinstance(result, dict) or not isinstance(result.get("content"), str):
                raise ValueError("invalid export")
            # No Content-Disposition interpolation; the browser receives a safe basename.
            extension = "json" if fmt == "json" else "md"
            return {"filename": f"dsh-session.{extension}", "content": result["content"],
                    "content_type": "application/json;charset=utf-8" if fmt == "json" else "text/markdown;charset=utf-8"}
        return await invoke(operation)

    async def preview():
        async def operation():
            sid = _sid(request.query.get("id"))
            cursor = request.query.get("cursor", "")
            if not isinstance(cursor, str) or len(cursor) > 64:
                raise ValueError("invalid cursor")
            result = await plugin.preview_managed_session(sid, cursor)
            # Explicit response allowlist: no ingress address or configuration.
            return {key: result[key] for key in (
                "session_id", "title", "source", "messages", "next_cursor", "complete", "notice")}
        return await invoke(operation)

    async def runtime():
        async def operation():
            sid = _sid(request.query.get('id'))
            record = plugin.session_store.get(sid)
            if record is None:
                raise KeyError(sid)
            data, _ = await plugin._management_request('/sessions/runtime', {
                'umo': record['umo'], 'sessionId': record['session_id'], 'detail': '1'})
            return data
        return await invoke(operation)

    async def scan_connection():
        return await invoke(plugin.scan_connections)

    async def apply_connection():
        async def operation():
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'id'} or not isinstance(body['id'], str):
                raise ValueError('invalid scan candidate')
            return await plugin.apply_connection_scan(body['id'])
        return await invoke(operation)

    async def test_connection():
        return await invoke(plugin.test_connectivity)

    async def diagnostic_export():
        async def operation():
            return plugin.export_diagnostics()
        return await invoke(operation)

    routes = [
        (f"/{PLUGIN_NAME}/connection/test", test_connection, ["POST"], "手动测试已保存DSH连接"),
        (f"/{PLUGIN_NAME}/diagnostics/export", diagnostic_export, ["GET"], "导出脱敏运行与错误日志"),
        (f"/{PLUGIN_NAME}/connection/scan", scan_connection, ["POST"], "测试功能：手动扫描DSH连接"),
        (f"/{PLUGIN_NAME}/connection/apply", apply_connection, ["POST"], "测试功能：确认保存扫描连接"),
        (f"/{PLUGIN_NAME}/sessions/runtime", runtime, ["GET"], "DSH 只读运行证据"),
        (f"/{PLUGIN_NAME}/sessions/preview", preview, ["GET"], "DSH 分页预览会话正文"),
        (f"/{PLUGIN_NAME}/sessions", sessions, ["GET"], "DSH 会话列表"),
        (f"/{PLUGIN_NAME}/sessions/refresh", refresh, ["POST"], "DSH 刷新运行状态"),
        (f"/{PLUGIN_NAME}/sessions/update", update, ["POST"], "DSH 独立保留与回收开关"),
        (f"/{PLUGIN_NAME}/sessions/export", export, ["GET"], "DSH 导出会话记录"),
    ]
    for route in routes:
        register(*route)
    plugin._dsh_webui_routes = routes
    return True


def unregister_webui(plugin: Any) -> None:
    """Remove only handlers owned by this instance; preserve replacement handlers."""
    owned = getattr(plugin, "_dsh_webui_routes", ())
    registered = getattr(plugin.context, "registered_web_apis", None)
    if isinstance(registered, list):
        handlers = {item[1] for item in owned}
        registered[:] = [item for item in registered if item[1] not in handlers]
    plugin._dsh_webui_routes = []
