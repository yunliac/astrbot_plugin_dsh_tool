"""Experimental, administrator-triggered local discovery. Never returns secrets."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

# Read only administrator-owned standard paths visible to this AstrBot process.
TOKEN_PATHS = (
    Path('/root/.dsh/dsh-astrbot-ingress/config.json'),
    Path('/home/astrbot/.dsh/dsh-astrbot-ingress/config.json'),
    Path('/AstrBot/data/dsh-ingress/config.json'),
)
if os.name == 'nt':
    TOKEN_PATHS = (Path.home() / '.dsh' / 'dsh-astrbot-ingress' / 'config.json',) + TOKEN_PATHS
LOCAL_BASES = ('http://127.0.0.1:3188', 'http://host.docker.internal:3188',
               'http://172.17.0.1:3188', 'http://[::1]:3188')


def valid_base(value):
    """Return a safe HTTP(S) base, or None; malformed administrator input is not fatal."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    # Reject parser disagreements (notably backslashes and silently removed controls),
    # userinfo, and query/fragment delimiters that would swallow appended route paths.
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value) or '\\' in value or '?' in value or '#' in value:
        return None
    try:
        p = urlsplit(value)
        port = p.port  # Validate numeric/range errors, not just the hostname.
        if (p.scheme not in ('http', 'https') or not p.hostname
                or p.username is not None or p.password is not None
                or p.netloc.endswith(':') or port == 0):
            return None
    except ValueError:
        return None
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip('/'), '', ''))


class ConnectionScanMixin:
    async def _probe_connection(self, base, token):
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
                async with client.stream('GET', base + '/sessions/managed',
                        headers={'Authorization': 'Bearer ' + token}) as response:
                    if response.status_code != 200:
                        return False
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1024 * 1024:
                            return False
                    result = json.loads(data)
                    return isinstance(result, dict) and result.get('ok') is True and isinstance(result.get('sessions'), list)
        except Exception:
            return False

    async def scan_connections(self):
        lock = getattr(self, '_connection_scan_lock', None)
        if lock is None:
            lock = self._connection_scan_lock = asyncio.Lock()
        if lock.locked():
            raise ValueError('scan in progress')
        async with lock:
            if time.monotonic() - getattr(self, '_connection_scan_at', -1000) < 10:
                raise ValueError('scan cooldown')
            self._connection_scan_at = time.monotonic()
            self._connection_scan_pending = {}
            bases, current, _, _ = self._resolve_settings()
            tokens = []
            if current:
                tokens.append((current, '当前插件配置或环境变量'))
            for path in TOKEN_PATHS:
                try:
                    if not path.is_file() or path.stat().st_size > 65536:
                        continue
                    obj = json.loads(path.read_text(encoding='utf-8-sig'))
                    token = obj.get('token') if isinstance(obj, dict) else None
                    if isinstance(token, str) and token and not any(ord(ch) < 32 or ord(ch) == 127 for ch in token) and len(token) <= 8192 and not any(t[0] == token for t in tokens):
                        tokens.append((token, '本机可读 DSH 标准配置'))
                except (OSError, ValueError, TypeError):
                    pass
            # Tokens from files never go to arbitrary configured remote addresses.
            configured = [normalized for b in bases if (normalized := valid_base(b))]
            candidates = list(dict.fromkeys(configured + list(LOCAL_BASES)))[:6]
            found = []
            deadline = time.monotonic() + 12
            for base in candidates:
                for token, source in tokens[:4]:
                    if time.monotonic() >= deadline:
                        break
                    if base not in LOCAL_BASES and token != current:
                        continue
                    if await self._probe_connection(base, token):
                        identifier = secrets.token_urlsafe(24)
                        self._connection_scan_pending[identifier] = (base, token, time.monotonic())
                        found.append({'id': identifier, 'ingress_url': base, 'token': '已找到（不显示）', 'source': source})
                        break
            return {'experimental': True, 'candidates': found,
                    'notice': '测试功能：仅验证已配置地址和已知本机/容器地址，不扫描局域网。容器无法读取宿主机文件时，请手填 token 或只读挂载 /AstrBot/data/dsh-ingress/config.json。未发现不代表服务不存在。结果5分钟内有效，点击应用才保存。'}

    async def apply_connection_scan(self, identifier):
        lock = getattr(self, '_connection_scan_lock', None)
        if lock is None:
            raise ValueError('scan required')
        async with lock:
            candidate = getattr(self, '_connection_scan_pending', {}).get(identifier)
            if not candidate or time.monotonic() - candidate[2] > 300:
                raise ValueError('expired scan')
            base, token, _ = candidate
            if not await self._probe_connection(base, token):
                raise ValueError('connection changed')
            save = getattr(self.config, 'save_config', None)
            if not callable(save):
                raise ValueError('config persistence unavailable')
            before = dict(self.config)
            try:
                self.config['ingress_url'] = base
                self.config['token'] = token
                save()
            except Exception:
                self.config.clear()
                self.config.update(before)
                raise
            self._connection_scan_pending = {}
            return {'saved': True, 'ingress_url': base, 'token': '已保存（不显示）', 'experimental': True}
