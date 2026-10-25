"""Bounded, redacted plugin diagnostics and administrator-only connectivity tests."""
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import threading
import time

import httpx


def redact(value):
    text = str(value)
    text = re.sub(r'(?im)^.*(?:authorization|bearer|api[_-]?key|password|secret|token)\s*[\"\x27]*\s*[:=].*$', '[REDACTED CREDENTIAL LINE]', text)
    text = re.sub(r'(?i)Bearer\s+[^\s\"\x27<>]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED KEY]', text)
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'(?i)([?&](?:token|key|password|secret)=)[^&\s]+', r'\1[REDACTED]', text)
    return text[:4000]


class DiagnosticLog(logging.Handler):
    """Plugin-only JSONL journal, capped 8 MiB, error retention 72h."""
    def __init__(self, path):
        super().__init__(logging.INFO)
        self.path = Path(path)
        self.guard = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record):
        try:
            row = {'time': record.created, 'level': record.levelname,
                   'message': redact(record.getMessage())}
            if record.exc_info:
                row['error'] = redact(logging.Formatter().formatException(record.exc_info))
            with self.guard:
                if self.path.exists() and self.path.stat().st_size > 8 * 1024 * 1024:
                    self._compact()
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass
        except Exception:
            # Never recurse through logging on a failing diagnostic disk.
            pass

    def _rows(self):
        if not self.path.exists():
            return []
        rows = []
        with self.path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row.get('time'), (int, float)):
                        rows.append(row)
                except (ValueError, AttributeError):
                    continue
        return rows

    def _compact(self):
        cutoff = time.time() - 72 * 3600
        rows = [r for r in self._rows() if r['time'] >= cutoff][-500:]
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows), encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def export(self):
        now = time.time()
        with self.guard:
            self._compact()
            selected = [r for r in self._rows() if now - r['time'] <=
                        (72 if r.get('level') in ('ERROR', 'CRITICAL') else 24) * 3600]
        lines = ['DSH 插件诊断日志', '范围：近24小时运行信息、近72小时错误；UTC时间。日志从功能启用后开始收集，容量限制可能提前淘汰旧记录。',
                 '不包含完整对话、任务正文及token；脱敏非绝对保证，分享前请检查。', '']
        for row in selected:
            lines.append(f"{datetime.fromtimestamp(row['time'], timezone.utc).isoformat()} [{row.get('level')}] {redact(row.get('message', ''))}")
            if row.get('error'):
                lines.append(redact(row['error']))
        return '\n'.join(lines)


class DiagnosticsMixin:
    def _setup_diagnostics(self):
        self._diagnostic_handler = None
        try:
            from astrbot.api.star import StarTools
            path = StarTools.get_data_dir('astrbot_plugin_dsh_tool') / 'diagnostics.jsonl'
            handler = DiagnosticLog(path)
            # AstrBot custom logger implements normal handler dispatch.
            target = getattr(self, 'logger', None)
            if not hasattr(target, 'addHandler'):
                target = logging.getLogger('astrbot_plugin_dsh_tool.diagnostics')
            target.setLevel(logging.INFO)
            target.addHandler(handler)
            self._diagnostic_target = target
            self._diagnostic_handler = handler
            self._diag('info', '诊断日志启动')
        except Exception:
            pass

    def _diag(self, level, message):
        target = getattr(self, '_diagnostic_target', None)
        if target:
            getattr(target, level)(redact(message))

    def timeout_diagnostic(self):
        try:
            config = self.context.get_config()
            limit = float(config.get('agent_runner', {}).get('config', {}).get('misc', {}).get('tool_call_timeout', 120))
            waiting = float(self.config.get('timeout_sec', 900))
            return {'astrbot_tool_timeout': limit, 'plugin_idle_timeout': waiting,
                    'warning': ('AstrBot工具超时短于插件等待，前台长任务可能被提前中止。建议使用background=true或/dsh bg；如需前台等待，请手动调大AstrBot工具超时。插件等待按流空闲计时，持续输出时总时长可超过此值。'
                                if limit <= waiting else '插件按流空闲计时，AstrBot按工具总时长计时；长任务优先后台执行。')}
        except Exception:
            return {'warning': '无法读取AstrBot工具超时；长任务建议/dsh bg或background=true，避免聊天模型工具等待超时。'}

    async def test_connectivity(self):
        now = time.monotonic()
        if now - getattr(self, '_connection_test_at', -1000) < 5:
            raise ValueError('test cooldown')
        self._connection_test_at = now
        bases, token, _, _ = self._resolve_settings()
        if not token:
            return {'ok': False, 'results': [], 'message': '未填写token，请保存插件配置后重试。'}
        results = []
        targets = [(s, b + path) for b in bases[:2]
                   for s, path in [('ingress', '/health'), ('management', '/sessions/managed')]]
        try:
            targets += [('runtime', b + '/health') for b in self._runtime_bases(bases)[:2]]
        except ValueError:
            results.append({'service': 'runtime', 'ok': False, 'message': '进度观察地址格式错误。'})
        for service, url in targets:
            start = time.monotonic()
            message, ok = '', False
            try:
                async with httpx.AsyncClient(timeout=4, trust_env=False, follow_redirects=False) as client:
                    async with client.stream('GET', url, headers={'Authorization': 'Bearer '+token}) as response:
                        if response.status_code in (401, 403):
                            message = '认证失败：token不匹配或无权限。'
                        elif response.status_code == 404:
                            message = ('管理扩展接口缺失：原版ingress仍可执行任务，但活跃列表/会话管理需要兼容管理接口。不是token错误。'
                                       if service == 'management' else '接口不存在：地址/端口错误或服务版本缺少此接口。')
                        elif 300 <= response.status_code < 400:
                            message = '服务返回重定向；为避免凭据泄露未跟随。'
                        elif response.status_code != 200:
                            message = f'HTTP {response.status_code}，服务返回异常。'
                        else:
                            raw = bytearray()
                            async for part in response.aiter_bytes():
                                raw.extend(part)
                                if len(raw) > 1024 * 1024:
                                    raise ValueError('oversize response')
                            data = json.loads(raw)
                            ok = isinstance(data, dict) and (
                                (data.get('ok') is True or data.get('status') == 'ok') if service == 'ingress' else
                                (data.get('ok') is True and (isinstance(data.get('sessions'), list)
                                 if service == 'management' else data.get('service') == 'dsh-runtime-observer')))
                            message = ('健康端点可达；健康接口不一定验证token，管理接口成功才确认管理认证。' if service == 'ingress' else '连接和认证正常。') if ok else '响应格式不匹配，可能填入DSH网页端口而非ingress端口。'
            except httpx.TimeoutException:
                message = '连接超时：检查防火墙、地址、监听接口及Docker网络。'
            except httpx.ConnectError:
                message = '无法建立连接：检查服务启动、域名解析与端口；容器中的127.0.0.1不是宿主机。'
            except Exception:
                message = '地址、凭据编码或响应格式无效，请检查已保存设置。'
            results.append({'service': service, 'ok': ok, 'elapsed_ms': int((time.monotonic()-start)*1000), 'message': message})
            self._diag('info' if ok else 'error', f'连通测试 {service}: {message}')
        return {'ok': any(r['ok'] for r in results if r['service'] == 'ingress'), 'results': results,
                'timeout': self.timeout_diagnostic(),
                'message': '只测试已保存配置，未提交任务。健康可达不等于所有功能或token已验证；请分别检查管理和观察接口。' + self.timeout_diagnostic()['warning']}

    def export_diagnostics(self):
        handler = getattr(self, '_diagnostic_handler', None)
        content = handler.export() if handler else '诊断日志未能初始化，请检查插件数据目录权限。'
        # Exact configured secret is removed again at export boundary.
        try:
            token = self._resolve_settings()[1]
            if token:
                content = content.replace(token, '[REDACTED]')
        except Exception:
            pass
        return {'filename': 'dsh-diagnostics.txt', 'content': content}
