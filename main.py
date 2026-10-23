"""在日常对话中给模型一个 dsh_task 工具，把重活交给本机 DSH。

普通聊天时由模型自行判断是否调用 ``dsh_task``，把「写代码 / 执行命令 /
读写服务器文件 / 跑长任务」转交给本机运行的 DeepSeek Harness (DSH)，
再把最终结果拿回来继续回答。

支持多会话并行：
- ``session`` 指定通道：留空走当前对话的默认会话，填名字走具名会话，
  填 ``new`` 则每次新开一个独立会话；
- ``background=True`` 时立即返回，任务在独立会话里继续跑，完成后把结果
  直接发回聊天，因此同一对话里也能同时跑多个任务。

与 ``astrbot_plugin_dsh``（/dsh 命令桥）互补：那个是显式命令，这个是隐式工具。
本插件直接复用它的 ingress 地址与 token 配置，也可以完全独立配置。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register

try:  # AstrBot >= 4.16
    from astrbot.core.utils.astrbot_path import get_astrbot_config_path
except Exception:  # pragma: no cover - 兼容旧版本
    get_astrbot_config_path = None  # type: ignore[assignment]


PLUGIN_NAME = "astrbot_plugin_dsh_tool"
BRIDGE_PLUGIN_NAME = "astrbot_plugin_dsh"

DEFAULT_INGRESS = "http://host.docker.internal:3188"
FALLBACK_INGRESS = "http://127.0.0.1:3188"
DEFAULT_TIMEOUT_SEC = 900
DEFAULT_MAX_RESULT_CHARS = 6000
DEFAULT_MAX_PARALLEL = 3

TRAILING_MARK = re.compile(r"\s*——\s*本回合结束\s*$")
LANE_INVALID = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")
TRUNCATE_SUFFIX = "\n...(结果过长，已截断)"
CHUNK_SIZE = 1800


# --------------------------------------------------------------------------- #
# 配置读取
# --------------------------------------------------------------------------- #


def _load_bridge_config(explicit_path: str = "") -> dict[str, Any]:
    """读取 astrbot_plugin_dsh 的配置文件，找不到就返回空字典。"""
    candidates: list[str] = []

    if explicit_path.strip():
        candidates.append(os.path.expanduser(explicit_path.strip()))
    env_path = os.environ.get("DSH_BRIDGE_CONFIG", "").strip()
    if env_path:
        candidates.append(os.path.expanduser(env_path))
    if get_astrbot_config_path is not None:
        try:
            candidates.append(
                os.path.join(
                    get_astrbot_config_path(),
                    f"{BRIDGE_PLUGIN_NAME}_config.json",
                )
            )
        except Exception:  # pragma: no cover - 路径解析失败时忽略
            pass
    # 兼容 AstrBot 官方 Docker 镜像的默认位置
    candidates.append(f"/AstrBot/data/config/{BRIDGE_PLUGIN_NAME}_config.json")

    seen: set[str] = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            logger.debug("[dsh_task] 复用桥配置：%s", path)
            return data
    return {}


def _conf(config: Any, key: str, default: Any = None) -> Any:
    """从插件配置里取一个值，兼容 AstrBotConfig / dict / None。"""
    if config is None:
        return default
    try:
        value = config.get(key, default) if hasattr(config, "get") else getattr(
            config, key, default
        )
    except Exception:  # pragma: no cover - 配置对象异常时保守回退
        return default
    return default if value is None else value


def _pick(*values: Any, default: str = "") -> str:
    """按顺序取第一个非空值。"""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _as_positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _as_positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
    return default


def _normalize_base(url: str) -> str:
    """补全协议、去掉末尾斜杠。"""
    text = (url or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    return text


def _clean(text: str, max_chars: int) -> str:
    text = TRAILING_MARK.sub("", text or "").strip()
    if len(text) > max_chars:
        text = text[:max_chars] + TRUNCATE_SUFFIX
    return text


def _short_id(session_id: str) -> str:
    return str(session_id or "").replace("session-", "")[:8]


def _lane_umo(base_umo: str, session: str) -> tuple[str, str]:
    """把 session 参数映射成 (umo, 通道名)。

    - 留空 / auto：默认会话（与以前行为一致）
    - new：每次生成唯一 umo → ingress 新建一个 DSH 会话
    - 其他名字：同一个名字始终对应同一个 DSH 会话，不同名字互不干扰
    """
    raw = (session or "").strip()
    if not raw or raw.lower() in {"auto", "default"}:
        return base_umo, ""
    if raw.lower() in {"new", "fresh"}:
        return f"{base_umo}#new-{uuid.uuid4().hex[:8]}", "new"

    slug = LANE_INVALID.sub("-", raw)[:32].strip("-")
    if not slug:
        return base_umo, ""
    return f"{base_umo}#{slug}", slug


def _chunk_text(text: str, limit: int = CHUNK_SIZE) -> list[str]:
    """按换行切成不太长的段，避免单条消息超平台上限。"""
    text = text or ""
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


# --------------------------------------------------------------------------- #
# DSH ingress 调用
# --------------------------------------------------------------------------- #


async def _call_ingress(
    base: str,
    token: str,
    payload: dict[str, Any],
    timeout: float,
    max_chars: int,
) -> tuple[str, str | None, str]:
    """POST /inbound，消费 SSE 流，返回 (文本结果, 错误信息, 会话 id)。"""
    headers = {"Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    chunks: list[str] = []
    error: str | None = None
    session_id = ""
    finished = False
    event_name: str | None = None
    data_lines: list[str] = []

    def _apply(event: str, raw: str) -> bool:
        """处理一个 SSE 事件；返回 True 表示收到 done。"""
        nonlocal error, session_id
        if event in {"ack", "done"}:
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {}
            if isinstance(data, dict) and data.get("sessionId"):
                session_id = str(data["sessionId"])
            return event == "done"
        if event == "error":
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {}
            if isinstance(data, dict):
                error = (
                    data.get("message")
                    or data.get("error")
                    or raw
                    or "unknown error"
                )
            else:
                error = raw or "unknown error"
            return False
        if event != "text" or not raw:
            return False
        try:
            data = json.loads(raw)
        except ValueError:
            chunks.append(raw)
            return False
        text = data.get("text") if isinstance(data, dict) else None
        if text:
            chunks.append(str(text))
        return False

    timeout_obj = httpx.Timeout(
        None,
        connect=10.0,
        read=timeout,
        write=30.0,
        pool=30.0,
    )
    # trust_env=False：容器里的 HTTP_PROXY 会把 host.docker.internal 拐去代理
    async with httpx.AsyncClient(timeout=timeout_obj, trust_env=False) as client:
        async with client.stream(
            "POST",
            f"{base}/inbound",
            json=payload,
            headers=headers,
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                hint = ""
                if resp.status_code in (401, 403):
                    hint = "（请检查 ingress 地址与 token 配置）"
                return "", f"HTTP {resp.status_code}{hint}：{body}", ""

            async for raw_line in resp.aiter_lines():
                line = raw_line.rstrip("\r")
                if not line:
                    if event_name and data_lines:
                        finished = _apply(event_name, "\n".join(data_lines))
                        if finished:
                            break
                    event_name, data_lines = None, []
                    continue
                if line.startswith(":"):
                    continue
                field, sep, value = line.partition(":")
                if not sep:
                    continue
                if field == "event":
                    # 兼容没有空行分隔的流
                    if event_name and data_lines:
                        finished = _apply(event_name, "\n".join(data_lines))
                        if finished:
                            break
                    event_name = value.strip()
                    data_lines = []
                elif field == "data":
                    data_lines.append(value[1:] if value.startswith(" ") else value)

            if not finished and event_name and data_lines:
                _apply(event_name, "\n".join(data_lines))

    return _clean("\n".join(chunks), max_chars), error, session_id


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #


@register(PLUGIN_NAME, "yunliac", "在日常对话中按需调用本机 DSH 的 LLM 工具", "1.2.0")
class DshToolPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._bg_lanes: set[str] = set()
        self._session_activity: dict[str, dict[str, Any]] = {}
        self._active_lanes: dict[str, int] = {}
        self._lane_locks: dict[str, asyncio.Lock] = {}
        self._reaper_task: asyncio.Task | None = None
        self._reclaim_unsupported: set[str] = set()
        self._closing = False

    def _reclaim_enabled(self) -> bool:
        return _as_bool(_conf(self.config, "idle_reclaim_enabled", False))

    def _idle_timeout(self) -> float:
        return max(60, min(604800, _as_positive_int(
            _conf(self.config, "idle_timeout_sec", 1800), 1800)))

    def _reclaim_interval(self) -> float:
        return max(10.0, min(3600.0, _as_positive_float(
            _conf(self.config, "idle_check_interval_sec", 60), 60)))

    async def initialize(self) -> None:
        self._ensure_reaper()

    def _ensure_reaper(self) -> None:
        if self._closing or not self._reclaim_enabled():
            return
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _reaper_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(self._reclaim_interval())
            try:
                await self._reclaim_idle_sessions()
            except Exception:
                logger.exception("[dsh_task] 空闲会话检查失败，下轮重试")

    async def _reclaim_idle_sessions(self) -> None:
        if not self._reclaim_enabled() or self._closing:
            return
        idle = self._idle_timeout()
        for umo, record in list(self._session_activity.items()):
            if self._closing or not self._reclaim_enabled():
                return
            if self._active_lanes.get(umo, 0) or umo in self._bg_lanes:
                continue
            if time.monotonic() - record["last_used"] < idle:
                continue
            lock = self._lane_locks.setdefault(umo, asyncio.Lock())
            async with lock:
                if (self._active_lanes.get(umo, 0) or umo in self._bg_lanes
                        or self._session_activity.get(umo) is not record
                        or time.monotonic() - record["last_used"] < idle):
                    continue
                base = record["base"]
                if base in self._reclaim_unsupported:
                    continue
                # Resolve current credentials; never persist tokens in activity records.
                bases, token, _, _ = self._resolve_settings()
                if base not in bases:
                    continue
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                try:
                    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                        response = await client.post(
                            f"{base}/session/release", headers=headers,
                            json={"umo": umo, "sessionId": record["session_id"],
                                  "idle_seconds": idle})
                    if response.status_code in (404, 405, 501):
                        self._reclaim_unsupported.add(base)
                        logger.warning("[dsh_task] ingress 不支持安全空闲释放；已跳过。"
                                       "请安装配套兼容补丁并重载本插件。")
                        continue
                    if response.status_code == 409:
                        continue  # Busy, not owned, or binding changed: fail closed.
                    response.raise_for_status()
                    result = response.json()
                    if (isinstance(result, dict)
                            and result.get("ok") is True and result.get("released") is True
                            and result.get("sessionId") == record["session_id"]):
                        self._session_activity.pop(umo, None)
                        if not self._active_lanes.get(umo, 0):
                            self._lane_locks.pop(umo, None)
                        logger.info("[dsh_task] 已回收空闲会话：%s", _short_id(record["session_id"]))
                except (httpx.HTTPError, ValueError):
                    logger.warning("[dsh_task] 空闲释放失败，保留记录下轮重试", exc_info=True)

    async def terminate(self) -> None:
        """插件卸载 / 重载时取消还在跑的后台任务。"""
        self._closing = True
        tasks = list(self._bg_tasks)
        if self._reaper_task is not None:
            tasks.append(self._reaper_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_lanes.clear()

    @filter.on_llm_request()
    async def remind_dsh_capability(self, event: AstrMessageEvent, req: Any) -> None:
        """Describe current capabilities, not stale assistant denials in chat history.

        Never add a tool that persona/session configuration has excluded.
        """
        tools = getattr(req, "func_tool", None)
        tool = tools.get_tool("dsh_task") if tools is not None else None
        allowed, _ = self._check_permission(event)
        available = tool is not None and getattr(tool, "active", True)
        logger.info("[dsh_task] 请求工具诊断：available=%s permitted=%s", available, allowed)
        if not available or not allowed:
            return
        hint = (
            "\n[当前 DSH 工具能力]\n"
            "本次请求实际提供了 dsh_task 函数工具。以当前工具列表为准，"
            "不要沿用历史回复中‘没有 dsh_task/终端通道’的过期结论。"
            "用户明确要求调用 DSH、检查服务器实时状态或实际操作时，应调用 dsh_task，"
            "不要让用户代为执行命令或反复确认工具是否存在。"
            "只询问能否连接时可用 task='/status' 检查；"
            "要查服务器软件/日志/配置必须传完整任务让 DSH 实际执行。"
            "不要声称执行成功，除非工具结果证实。并行时使用不同 session 和 background=true。\n"
        )
        req.system_prompt = (getattr(req, "system_prompt", None) or "") + hint

    # -- 权限 ------------------------------------------------------------- #

    def _check_permission(self, event: AstrMessageEvent) -> tuple[bool, str]:
        allow_users = _conf(self.config, "allow_users", []) or []
        if not isinstance(allow_users, (list, tuple, set)):
            allow_users = [allow_users]
        allow_set = {str(item).strip() for item in allow_users if str(item).strip()}

        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
        if sender and sender in allow_set:
            return True, ""

        if not bool(_conf(self.config, "admin_only", True)):
            return True, ""

        try:
            if event.is_admin():
                return True, ""
        except Exception:
            logger.warning("[dsh_task] 无法判断管理员身份", exc_info=True)
            return False, "无法确认管理员身份，已拒绝调用 DSH 工具。"
        return False, "只有管理员可以使用 DSH 工具（dsh_task）。"

    # -- 配置 ------------------------------------------------------------- #

    def _resolve_settings(self) -> tuple[list[str], str, float, int]:
        bridge_path = str(_conf(self.config, "bridge_config_path", "") or "")
        bridge = _load_bridge_config(bridge_path)

        base = _normalize_base(
            _pick(
                _conf(self.config, "ingress_url", ""),
                os.environ.get("DSH_INGRESS_URL"),
                bridge.get("ingress_url"),
            )
        )
        token = _pick(
            _conf(self.config, "token", ""),
            os.environ.get("DSH_INGRESS_TOKEN"),
            bridge.get("token"),
        )

        timeout = _as_positive_float(
            _pick(
                _conf(self.config, "timeout_sec", ""),
                bridge.get("timeout_sec"),
                default=str(DEFAULT_TIMEOUT_SEC),
            ),
            DEFAULT_TIMEOUT_SEC,
        )
        timeout = max(30.0, min(timeout, 3600.0))

        max_chars = _as_positive_int(
            _pick(
                _conf(self.config, "max_result_chars", ""),
                default=str(DEFAULT_MAX_RESULT_CHARS),
            ),
            DEFAULT_MAX_RESULT_CHARS,
        )
        max_chars = max(500, min(max_chars, 20000))

        if base:
            bases = [base]
        else:
            # 没配置时按「Docker 常用 → 本机」依次尝试
            bases = [DEFAULT_INGRESS, FALLBACK_INGRESS]

        return bases, token, timeout, max_chars

    def _max_parallel(self) -> int:
        value = _as_positive_int(
            _conf(self.config, "max_parallel", DEFAULT_MAX_PARALLEL),
            DEFAULT_MAX_PARALLEL,
        )
        return max(1, min(value, 20))

    # -- 调用 ------------------------------------------------------------- #

    async def _dispatch(
        self, bases: list[str], token: str, payload: dict[str, Any],
        timeout: float, max_chars: int,
    ) -> tuple[str, str]:
        self._ensure_reaper()
        umo = str(payload.get("umo", ""))
        if not umo:
            return await self._dispatch_once(bases, token, payload, timeout, max_chars)
        # Count queued calls too: the collector must never slip in between turns.
        self._active_lanes[umo] = self._active_lanes.get(umo, 0) + 1
        lock = self._lane_locks.setdefault(umo, asyncio.Lock())
        try:
            async with lock:
                return await self._dispatch_once(bases, token, payload, timeout, max_chars)
        finally:
            if umo in self._session_activity:
                self._session_activity[umo]["last_used"] = time.monotonic()
            remaining = self._active_lanes[umo] - 1
            if remaining:
                self._active_lanes[umo] = remaining
            else:
                self._active_lanes.pop(umo, None)
                if umo not in self._session_activity:
                    self._lane_locks.pop(umo, None)

    async def _dispatch_once(
        self,
        bases: list[str],
        token: str,
        payload: dict[str, Any],
        timeout: float,
        max_chars: int,
    ) -> tuple[str, str]:
        failures: list[str] = []
        for base in bases:
            try:
                text, error, session_id = await _call_ingress(
                    base, token, payload, timeout, max_chars
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                failures.append(f"{base}（{exc.__class__.__name__}）")
                continue

            if session_id and payload.get("umo"):
                self._session_activity[str(payload["umo"])] = {
                    "session_id": session_id, "base": base,
                    "last_used": time.monotonic(),
                }
            if not text and error:
                return f"DSH 调用失败：{error}", session_id
            if not text:
                return "DSH 没有返回任何内容（可能任务被取消或超时）。", session_id
            if error:
                text = f"{text}\n\n[DSH 报告错误：{error}]"
            return text, session_id

        detail = "；".join(failures) if failures else "未知原因"
        return (
            f"DSH 连接失败：{detail}\n"
            "请确认 DSH 已启动、ingress 地址可访问，并在插件配置里填好 token。",
            "",
        )

    async def _send_to_chat(self, umo: str, text: str) -> None:
        for chunk in _chunk_text(text):
            try:
                await StarTools.send_message(
                    umo,
                    MessageChain(chain=[Plain(chunk)]),
                )
            except Exception:
                logger.exception("[dsh_task] 后台结果发送失败：%s", umo)
                return

    async def _run_background(
        self,
        *,
        ticket: str,
        umo: str,
        lane: str,
        lane_umo: str,
        bases: list[str],
        token: str,
        payload: dict[str, Any],
        timeout: float,
        max_chars: int,
    ) -> None:
        try:
            text, session_id = await self._dispatch(
                bases, token, payload, timeout, max_chars
            )
        except httpx.TimeoutException:
            text, session_id = (
                f"任务超时（{int(timeout)} 秒内没有收到完整结果），可能仍在 DSH 中运行。",
                "",
            )
        except Exception as exc:  # pragma: no cover - 兜底
            logger.exception("[dsh_task] 后台任务失败：%s", ticket)
            text, session_id = f"后台任务失败：{exc}", ""
        finally:
            self._bg_lanes.discard(lane_umo)

        failed = text.startswith(
            ("DSH 调用失败", "DSH 连接失败", "后台任务失败", "任务超时")
        )
        header = "【DSH 后台任务失败】" if failed else "【DSH 后台任务完成】"
        meta = [f"#{ticket}"]
        if lane:
            meta.append(f"通道 {lane}")
        if session_id:
            meta.append(f"会话 {_short_id(session_id)}")
        await self._send_to_chat(umo, f"{header} {' · '.join(meta)}\n\n{text}")

    @filter.llm_tool(name="dsh_task")
    async def dsh_task(
        self,
        event: AstrMessageEvent,
        task: str,
        session: str = "",
        background: bool = False,
    ) -> str:
        """把需要写代码、执行 shell 命令、读写服务器文件或运行长任务的工作交给本机的 DeepSeek Harness (DSH) 代理执行，并返回它的最终结果。

        普通闲聊、常识问答、单纯查资料不要调用本工具（查资料用 web_search）。
        只有当用户明确希望「实际动手」——改代码、跑命令、看服务器目录/日志、装环境、生成文件、执行多步任务时才调用。
        DSH 在它自己的工作区里运行，可以读写文件、执行命令、访问网络；任务描述要完整、独立，包含路径、约束与期望输出。
        本工具默认仅管理员可用，是否允许由插件配置决定。

        并行用法：用户要求同时做几件互不相关的事时，给每个任务起不同的 session 名字（或 session="new"），
        并设置 background=true 立即返回；这些任务会各自在独立的 DSH 会话里同时执行，完成后结果会直接发到聊天。
        同一个 session 名字对应同一个 DSH 会话，适合需要延续上下文的后续任务。

        Args:
            task(string): 完整、独立的任务描述，写明目标、路径、约束和期望结果
            session(string): 可选。留空=当前对话的默认 DSH 会话；new=为这次任务新开独立会话；其他短名字=具名会话，同名复用上下文、不同名可并行
            background(boolean): 可选。true=立即返回，任务在后台继续跑，完成后把结果发到聊天；false=等 DSH 跑完再回答
        """
        task = (task or "").strip()
        if not task:
            return "任务描述为空，未调用 DSH。"

        allowed, reason = self._check_permission(event)
        if not allowed:
            return reason

        try:
            umo = event.unified_msg_origin or ""
        except Exception:
            umo = ""
        if not umo:
            umo = f"tool:{event.get_sender_id()}"

        base_umo = f"tool:{umo}"
        lane_umo, lane = _lane_umo(base_umo, session)

        payload = {
            "umo": lane_umo,
            "text": task,
            "senderId": str(event.get_sender_id() or ""),
            "chatType": "group" if event.get_group_id() else "private",
        }
        group_id = str(event.get_group_id() or "")
        if group_id:
            payload["groupId"] = group_id

        bases, token, timeout, max_chars = self._resolve_settings()
        use_background = _as_bool(background, False)

        if use_background:
            limit = self._max_parallel()
            running = len(self._bg_tasks)
            if running >= limit:
                return (
                    f"后台并行任务已达上限（{limit} 个），请等现有任务完成，"
                    "或调大插件配置里的 max_parallel。"
                )
            if lane_umo in self._bg_lanes:
                return (
                    f"通道「{lane or '默认会话'}」已有后台任务在跑，"
                    "等它完成，或换一个 session 名字再开一路。"
                )
            ticket = uuid.uuid4().hex[:6]
            self._bg_lanes.add(lane_umo)
            bg = asyncio.create_task(
                self._run_background(
                    ticket=ticket,
                    umo=umo,
                    lane=lane,
                    lane_umo=lane_umo,
                    bases=bases,
                    token=token,
                    payload=payload,
                    timeout=timeout,
                    max_chars=max_chars,
                )
            )
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
            where = f"通道 {lane}" if lane else "默认会话"
            return (
                f"已提交 DSH 后台任务 #{ticket}（{where}），任务会独立执行，"
                "完成后结果会直接发到这个聊天。"
            )

        if lane_umo in self._bg_lanes:
            return (
                f"通道「{lane or '默认会话'}」已有后台任务在跑，"
                "等它完成，或换一个 session 名字。"
            )

        logger.info(
            "[dsh_task] 转发任务：umo=%s lane=%s timeout=%ss",
            lane_umo,
            lane or "-",
            int(timeout),
        )

        try:
            text, session_id = await self._dispatch(
                bases, token, payload, timeout, max_chars
            )
        except httpx.TimeoutException:
            return (
                f"DSH 调用超时（{int(timeout)} 秒内没有收到完整结果）。"
                "任务可能仍在 DSH 中运行，可以稍后到 DSH 查看。"
            )
        except httpx.HTTPError as exc:
            logger.error("[dsh_task] 调用 DSH 失败：%s", exc)
            return f"DSH 调用失败：{exc}"
        except Exception as exc:  # pragma: no cover - 兜底，避免把异常抛给 LLM
            logger.exception("[dsh_task] 调用 DSH 时出现未预期错误")
            return f"DSH 调用失败：{exc}"

        if lane and session_id and not text.startswith("DSH "):
            text += f"\n\n[DSH 会话 {_short_id(session_id)} · 通道 {lane}]"
        return text
