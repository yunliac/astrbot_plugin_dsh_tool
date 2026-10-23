"""在日常对话中给模型一个 dsh_task 工具，把重活交给本机 DSH。

普通聊天时由模型自行判断是否调用 ``dsh_task``，把「写代码 / 执行命令 /
读写服务器文件 / 跑长任务」转交给本机运行的 DeepSeek Harness (DSH)，
再把最终结果拿回来继续回答。

与 ``astrbot_plugin_dsh``（/dsh 命令桥）互补：那个是显式命令，这个是隐式工具。
本插件直接复用它的 ingress 地址与 token 配置，也可以完全独立配置。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

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

TRAILING_MARK = re.compile(r"\s*——\s*本回合结束\s*$")
TRUNCATE_SUFFIX = "\n...(结果过长，已截断)"


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


# --------------------------------------------------------------------------- #
# DSH ingress 调用
# --------------------------------------------------------------------------- #


async def _call_ingress(
    base: str,
    token: str,
    payload: dict[str, Any],
    timeout: float,
    max_chars: int,
) -> tuple[str, str | None]:
    """POST /inbound，消费 SSE 流，返回 (文本结果, 错误信息)。"""
    headers = {"Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    chunks: list[str] = []
    error: str | None = None
    finished = False
    event_name: str | None = None
    data_lines: list[str] = []

    def _apply(event: str, raw: str) -> bool:
        """处理一个 SSE 事件；返回 True 表示收到 done。"""
        nonlocal error
        if event == "done":
            return True
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
                return "", f"HTTP {resp.status_code}{hint}：{body}"

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

    return _clean("\n".join(chunks), max_chars), error


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #


@register(PLUGIN_NAME, "yunliac", "在日常对话中按需调用本机 DSH 的 LLM 工具", "1.0.0")
class DshToolPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

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

    # -- 调用 ------------------------------------------------------------- #

    async def _dispatch(
        self,
        bases: list[str],
        token: str,
        payload: dict[str, Any],
        timeout: float,
        max_chars: int,
    ) -> str:
        failures: list[str] = []
        for base in bases:
            try:
                text, error = await _call_ingress(
                    base, token, payload, timeout, max_chars
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                failures.append(f"{base}（{exc.__class__.__name__}）")
                continue

            if not text and error:
                return f"DSH 调用失败：{error}"
            if not text:
                return "DSH 没有返回任何内容（可能任务被取消或超时）。"
            if error:
                text = f"{text}\n\n[DSH 报告错误：{error}]"
            return text

        detail = "；".join(failures) if failures else "未知原因"
        return (
            f"DSH 连接失败：{detail}\n"
            "请确认 DSH 已启动、ingress 地址可访问，并在插件配置里填好 token。"
        )

    @filter.llm_tool(name="dsh_task")
    async def dsh_task(self, event: AstrMessageEvent, task: str) -> str:
        """把需要写代码、执行 shell 命令、读写服务器文件或运行长任务的工作交给本机的 DeepSeek Harness (DSH) 代理执行，并返回它的最终结果。

        普通闲聊、常识问答、单纯查资料不要调用本工具（查资料用 web_search）。
        只有当用户明确希望「实际动手」——改代码、跑命令、看服务器目录/日志、装环境、生成文件、执行多步任务时才调用。
        DSH 在它自己的工作区里运行，可以读写文件、执行命令、访问网络；任务描述要完整、独立，包含路径、约束与期望输出。
        本工具默认仅管理员可用，是否允许由插件配置决定。

        Args:
            task(string): 完整、独立的任务描述，写明目标、路径、约束和期望结果
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

        payload = {
            "umo": f"tool:{umo}",
            "text": task,
            "senderId": str(event.get_sender_id() or ""),
            "chatType": "group" if event.get_group_id() else "private",
        }
        group_id = str(event.get_group_id() or "")
        if group_id:
            payload["groupId"] = group_id

        bases, token, timeout, max_chars = self._resolve_settings()
        logger.info(
            "[dsh_task] 转发任务：umo=%s base=%s timeout=%ss",
            payload["umo"],
            bases[0],
            int(timeout),
        )

        try:
            return await self._dispatch(bases, token, payload, timeout, max_chars)
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
