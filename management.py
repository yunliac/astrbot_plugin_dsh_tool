"""Persistent session management shared by QQ tools and authenticated dashboard."""
from __future__ import annotations
import asyncio
import json
import time
from typing import Any
import httpx


class SessionManagementMixin:
    async def _management_request(self, path: str, params: dict | None = None) -> tuple[dict, str]:
        bases, token, _, _ = self._resolve_settings()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        last = "ingress unavailable"
        for base in bases:
            try:
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    resp = await client.get(base + path, params=params, headers=headers)
                if resp.status_code in (404, 405, 501):
                    raise RuntimeError("ingress 尚未安装会话管理接口补丁")
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict) or data.get("ok") is not True:
                    raise RuntimeError("ingress 返回无效会话数据")
                return data, base
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = type(exc).__name__
        raise RuntimeError(last)

    def _record_session(self, umo: str, sid: str, base: str, **fields: Any) -> dict:
        origin, _, channel = umo.removeprefix("tool:").partition("#")
        return self.session_store.upsert(umo, sid, base, origin=origin,
                                         channel=channel or "默认会话", **fields)

    async def refresh_managed_sessions(self) -> dict:
        available, message = False, ""
        try:
            data, base = await self._management_request("/sessions/managed")
            now = time.time()
            for remote in data.get("sessions", []):
                if not isinstance(remote, dict):
                    continue
                umo, sid = remote.get("umo"), remote.get("sessionId")
                if not isinstance(umo, str) or not umo.startswith("tool:") or not isinstance(sid, str):
                    continue
                activity = remote.get("lastActivity", remote.get("updatedAt", now * 1000))
                last_used = float(activity) / 1000 if isinstance(activity, (float, int)) else now
                record = self._record_session(umo, sid, base,
                    title=str(remote.get("title") or ""), cwd=str(remote.get("cwd") or ""),
                    status=str(remote.get("status") or "unknown"),
                    last_used=last_used, remote_seen_at=now)
                # Import cold/live bindings without waking agents. Server owns final idle checks.
                if umo not in self._session_activity and record.get("status") != "cold":
                    self._session_activity[umo] = {"base": base, "session_id": sid,
                        "last_used": time.monotonic() - max(0, now - last_used)}
            available = True
        except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
            message = "无法刷新运行状态：请检查 ingress 连通性及会话管理补丁。已保留本地目录。"
        records = self.session_store.list()
        for record in records:
            record.pop("base", None)  # ingress addresses and tokens never reach the browser.
            if not available:
                record["status"] = "unknown"
            elif self._active_lanes.get(record["umo"], 0) or record["umo"] in self._bg_lanes:
                record["status"] = "running"
            record["idle_remaining_sec"] = max(0, int(self._idle_timeout() - (time.time() - record.get("last_used", time.time()))))
        return {"sessions": records, "ingress_available": available, "message": message,
                "idle": {"enabled": self._reclaim_enabled(), "timeout_sec": self._idle_timeout()}}

    async def list_managed_sessions(self) -> dict:
        return await self.refresh_managed_sessions()

    async def update_managed_session(self, record_id: str, long_term=None, exempt=None, title=None) -> dict:
        for value in (long_term, exempt):
            if value is not None and not isinstance(value, bool):
                raise ValueError("开关必须是布尔值")
        if title is not None and (not isinstance(title, str) or len(title) > 120):
            raise ValueError("会话名称最长 120 字符")
        async with self._management_lock:
            result = self.session_store.set_flags(record_id, long_term=long_term, exempt=exempt, title=title)
        result.pop("base", None)
        return result

    async def preview_managed_session(self, record_id: str, cursor: str = "") -> dict:
        if not isinstance(cursor, str) or len(cursor) > 64:
            raise ValueError("invalid cursor")
        record = self.session_store.get(record_id)
        if record is None:
            raise KeyError(record_id)
        try:
            data, _ = await self._management_request("/sessions/preview", {
                "umo": record["umo"], "sessionId": record["session_id"], "cursor": cursor})
            if not isinstance(data.get("messages"), list):
                raise ValueError("invalid preview")
            return {"session_id": record["session_id"], "title": record.get("title", ""),
                    "source": "dsh", "messages": data["messages"],
                    "next_cursor": data.get("next_cursor"), "complete": data.get("complete") is True,
                    "notice": data.get("notice", "")}
        except (httpx.HTTPError, RuntimeError):
            # Never silently restart pagination against a different data source.
            if cursor and not cursor.startswith("local:"):
                raise RuntimeError("历史读取中断，请重新打开会话")
        if cursor and not cursor.startswith("local:"):
            raise ValueError("invalid cursor")
        offset = int(cursor.removeprefix("local:")) if cursor else 0
        if offset < 0:
            raise ValueError("invalid cursor")
        messages = []
        for exchange in self.session_store.transcript(record_id):
            messages.extend([{"role": "user", "content": exchange.get("task", "")},
                             {"role": "assistant", "content": exchange.get("result", "")}])
        end = offset + 50
        return {"session_id": record["session_id"], "title": record.get("title", ""),
                "source": "local_fallback", "messages": messages[offset:end],
                "next_cursor": f"local:{end}" if end < len(messages) else None,
                "complete": False,
                "notice": "远端历史不可用；这里只显示本地任务与返回摘要，不是完整 DSH 对话。"}

    async def export_managed_session(self, record_id: str, format: str = "markdown") -> dict:
        if format not in ("markdown", "json"):
            raise ValueError("导出格式只支持 markdown 或 json")
        record = self.session_store.get(record_id)
        if record is None:
            raise KeyError(record_id)
        source, truncated = "dsh", False
        try:
            data, _ = await self._management_request("/sessions/history",
                {"umo": record["umo"], "sessionId": record["session_id"]})
            messages = data.get("messages", [])
            if not isinstance(messages, list):
                raise ValueError("Invalid history")
            truncated = bool(data.get("truncated"))
        except (httpx.HTTPError, RuntimeError, ValueError):
            source = "local_fallback"
            messages = []
            for exchange in self.session_store.transcript(record_id):
                messages.extend([{"role": "user", "content": exchange.get("task", "")},
                                 {"role": "assistant", "content": exchange.get("result", "")}])
        header = {"session_id": record["session_id"], "title": record.get("title", ""),
                  "source": source, "truncated": truncated, "exported_at": time.time()}
        if format == "json":
            content = json.dumps({**header, "messages": messages}, ensure_ascii=False, indent=2)
            mime, ext = "application/json; charset=utf-8", "json"
        else:
            lines = ["# DSH 会话记录", "", f"会话：{record['session_id']}",
                     f"来源：{source}", f"截断：{truncated}", ""]
            if source == "local_fallback":
                lines += ["> 远端历史不可用；以下仅为插件记录的任务和返回摘要，不是完整 DSH 历史。", ""]
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role", "unknown"))
                lines.extend([f"## {role}", "", str(msg.get("content", "")), ""])
            content, mime, ext = "\n".join(lines), "text/markdown; charset=utf-8", "md"
        return {"filename": f"dsh-{record_id}.{ext}", "content": content, "content_type": mime}

    async def manage_session_for_event(self, event, action: str, session: str = "", long_term: bool | None = None,
                                       exempt: bool | None = None) -> str:
        allowed, reason = self._check_permission(event)
        if not allowed:
            return reason
        origin = str(event.unified_msg_origin or "")
        if not origin:
            return "无法确认当前聊天，未修改任何会话。"
        await self.refresh_managed_sessions()
        rows = [r for r in self.session_store.list() if r.get("origin") == origin]
        if action == "list":
            return json.dumps([{k: r.get(k) for k in ("id", "channel", "title", "session_id", "status", "long_term", "exempt")} for r in rows], ensure_ascii=False)
        matches = [r for r in rows if session and session in (r["id"], r["session_id"], r.get("channel"), r.get("title"))]
        if not session and len(rows) == 1:
            matches = rows
        if len(matches) != 1:
            return "目标会话不明确或不存在。请先 list，明确指定当前聊天的会话通道名或管理 ID；未修改任何标记。"
        record = matches[0]
        if action == "update":
            if long_term is None and exempt is None:
                return "请明确要修改长期保留还是免回收；未修改任何标记。"
            result = await self.update_managed_session(record["id"], long_term=long_term, exempt=exempt)
            return json.dumps({"ok": True, "channel": result.get("channel"), "long_term": result["long_term"], "exempt": result["exempt"]}, ensure_ascii=False)
        return "不支持的操作，只允许 list 或 update。"
