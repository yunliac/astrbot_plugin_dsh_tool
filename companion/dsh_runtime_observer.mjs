/** Independent Cordis runtime observer. Never imports or invokes ingress internals. */
import http from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { timingSafeEqual } from 'node:crypto';
import z from '@deepseek-ai/schemastery';

export const name = 'dsh-runtime-observer';
// This Cordis fork accepts required service names, not { required, optional } metadata.
export const inject = ['agents', 'sessionQuery', 'sessionPersistence'];
console.info('[dsh-runtime-observer] module loaded');
export const Config = z.object({
  host: z.string().default('172.17.0.1'),
  port: z.number().default(3190),
  tokenPath: z.string().default('/root/.dsh/dsh-astrbot-ingress/config.json'),
  bindingsPath: z.string().default('/root/.dsh/dsh-astrbot-ingress/sessions.json'),
});
const MAX_RESPONSE = 128 * 1024;
const MAX_COLD = 8 * 1024 * 1024;
const MAX_JSON = 2 * 1024 * 1024;
const defaults = { host: '172.17.0.1', port: 3190, tokenPath: '/root/.dsh/dsh-astrbot-ingress/config.json', bindingsPath: '/root/.dsh/dsh-astrbot-ingress/sessions.json' };
const fail = (status, code) => Object.assign(new Error(code), { status, code });
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
async function jsonFile(path) {
  if ((await stat(path)).size > MAX_JSON) throw fail(503, 'configuration_too_large');
  const value = await readFile(path, 'utf8');
  if (Buffer.byteLength(value) > MAX_JSON) throw fail(503, 'configuration_too_large');
  return JSON.parse(value);
}
export function clean(value, limit = 1000) {
  return String(value ?? '').replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, '').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/^.*(?:authorization["']?\s*:|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|credentials?)\b["']?\s*[:=]).*$/gim, '[REDACTED CREDENTIAL LINE]')
    .replace(/\bBearer\s+[^\s"'<>]+/gi, 'Bearer [REDACTED]')
    .replace(/\bsk-[A-Za-z0-9_-]+/g, '[REDACTED KEY]')
    .replace(/\btoken\s*[:=]\s*[^\s,;}]+/gi, 'token=[REDACTED]')
    .replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '').slice(0, limit);
}
function bodyText(message) {
  return clean((Array.isArray(message?.content) ? message.content : [])
    .filter(block => block?.type === 'text' && typeof block.text === 'string').map(block => block.text).join('\n'));
}
/** Fail closed on unknown binding schemas; only an exact tool-prefixed key grants access. */
export function boundSession(bindings, umo) {
  const key = umo.startsWith('tool:') ? umo : `tool:${umo}`;
  const table = plain(bindings?.sessions) ? bindings.sessions : bindings;
  const entry = plain(table) && Object.hasOwn(table, key) ? table[key] : undefined;
  return typeof entry === 'string' ? entry : typeof entry?.sessionId === 'string' ? entry.sessionId : undefined;
}
function evidence(events) {
  const messages = [], tools = [];
  // Reverse bounded extraction: never stringify messages, streams, reasoning or opaque metadata.
  for (let i = events.length - 1; i >= 0 && (messages.length < 12 || tools.length < 20); i--) {
    const e = events[i], d = e?.data;
    if (!d) continue;
    if (messages.length < 12 && (e.type === 'user/message' || e.type === 'assistant/message')) {
      const m = e.type === 'user/message' ? d : d.message;
      // Synthetic user-role injections are not human evidence.
      if (e.type === 'user/message' && !['user', 'user-rpc'].includes(m?.source?.kind)) continue;
      messages.push({ seq: e.seq, time: e.time, role: m?.role, text: bodyText(m) });
    }
    if (tools.length < 20 && e.type === 'tool/call') tools.push({ seq: e.seq, time: e.time, type: 'call', callId: d.callId, name: clean(d.name, 160), arguments: clean(d.arguments) });
    if (tools.length < 20 && e.type === 'tool/result') tools.push({ seq: e.seq, time: e.time, type: 'result', callId: d.message?.toolCallId, isError: d.message?.isError === true, text: bodyText(d.message) });
  }
  return { messages: messages.reverse(), tools: tools.reverse() };
}
function project(values) {
  const out = {};
  if (Object.hasOwn(values, 'todos')) out.todos = values.todos === null ? null : Array.isArray(values.todos) ? values.todos.slice(0, 100).map(t => ({ content: clean(t.content), status: ['pending', 'in_progress', 'completed'].includes(t.status) ? t.status : 'unknown' })) : null;
  if (Object.hasOwn(values, 'goal')) {
    const p = values.goal, g = p?.goal;
    out.goal = g ? { id: clean(g.id, 200), revision: g.revision, objective: clean(g.objective), phase: g.phase, maxGoalRounds: g.maxGoalRounds, roundsStarted: p.roundsStarted, createdAt: p.createdAt, updatedAt: p.updatedAt, ...(g.blockedReason ? { blockedReason: { code: clean(g.blockedReason.code, 100), message: clean(g.blockedReason.message) } } : {}) } : null;
  }
  return out;
}
export async function snapshot(ctx, sid, detail = true) {
  const observedAt = new Date().toISOString();
  const agent = ctx.agents.get(sid);
  if (!agent) {
    const stored = await ctx.sessionPersistence.stat(sid);
    if (!stored) throw fail(404, 'session_not_found');
    // Unknown physical size cannot prove a bounded cold read. Fail closed.
    if (!Number.isFinite(stored.sizeBytes) || stored.sizeBytes > MAX_COLD) throw fail(413, 'cold_session_size_unbounded');
  }
  const lease = await ctx.sessionQuery.observeSession(sid, { projectionMode: 'all' });
  let out;
  try {
    if (lease.header?.id !== sid) throw fail(409, 'observation_identity_mismatch');
    out = { ok: true, sessionId: sid, observedAt, source: lease.source, asOfSeq: lease.cursor, live: !!agent,
      status: agent?.status ?? 'not_live', uncertain: ['Cross-service observations are not atomic.', 'Missing projection keys mean unavailable capability; not empty state.'],
      projections: project(lease.projections?.values ?? {}), pendingInteractions: [], interactionsSupported: false };
    if (detail) out.evidence = evidence(lease.events);
  } finally { lease[Symbol.dispose](); }
  if (agent && ctx.agents.get(sid) !== agent) {
    out.uncertain.push('Agent changed during observation; live resources omitted.');
    return out;
  }
  const optional = key => typeof ctx.get === 'function' ? ctx.get(key) : ctx[key];
  const goals = optional('goals'), jobs = optional('jobs'), terminals = optional('terminals');
  out.capabilities = { jobs: !!jobs, terminals: !!terminals, goals: !!goals };
  if (agent && goals?.get) {
    try { const goal = goals.get(agent); if (goal && out.projections.goal) out.projections.goal.activation = goal.activation; }
    catch { out.uncertain.push('Live goal activation unavailable.'); }
  }
  out.jobs = [];
  try {
    for (const job of (jobs ? jobs.list(sid) : []).filter(j => j.owner === sid).slice(-20)) {
      const item = { id: job.id, kind: clean(job.kind, 100), label: clean(job.label), status: job.status, progress: clean(job.progress, 300), detail: clean(job.detail, 300), startedAt: job.startedAt, finishedAt: job.finishedAt };
      if (detail) {
        const read = jobs.readAt(job.id, Math.max(job.output.earliest, job.output.total - 8192), sid);
        item.output = { next: read.next, lossy: read.lossy, tailOnly: true, chunks: read.chunks.slice(-40).map(c => ({ at: c.at, text: clean(c.text), channel: c.channel, gapBefore: c.gapBefore })) };
      }
      out.jobs.push(item);
    }
  } catch { out.uncertain.push('Job observation incomplete or unavailable.'); }
  out.terminals = [];
  if (!jobs) out.uncertain.push('Jobs capability unavailable in this context.');
  if (!terminals) out.uncertain.push('Terminals capability unavailable in this context.');
  if (agent && terminals) {
    try {
      for (const terminal of terminals.list(agent).slice(-8)) {
        const item = { sessionId: terminal.sessionId, name: clean(terminal.name, 160), type: clean(terminal.type, 100), status: terminal.status?.kind === 'running' ? { kind: 'running' } : { kind: 'exited', exitCode: terminal.status?.exitCode, signal: clean(terminal.status?.signal, 30) } };
        if (detail) {
          const page = terminals.read(agent, terminal.sessionId, { offset: 0, count: 40 });
          item.output = { text: clean(page.text, 6000), totalLines: page.totalLines, lineBegin: page.lineBegin, lineEnd: page.lineEnd, truncated: page.truncated, tailOnly: true };
        }
        out.terminals.push(item);
      }
    } catch { out.uncertain.push('Terminal observation incomplete or unavailable.'); }
  }
  return out;
}
function boundedJson(value) {
  let encoded = JSON.stringify(value);
  if (Buffer.byteLength(encoded) <= MAX_RESPONSE) return encoded;
  // Preserve status and explicitly mark omitted evidence rather than returning invalid/truncated JSON.
  const minimal = { ok: true, sessionId: value.sessionId, observedAt: value.observedAt, source: value.source, status: value.status, live: value.live, asOfSeq: value.asOfSeq, truncated: true, uncertain: ['Response exceeded 128 KiB; evidence and resource details omitted.'] };
  return JSON.stringify(minimal);
}
async function requestBody(req) {
  let size = 0, chunks = [];
  for await (const chunk of req) { size += chunk.length; if (size > 16384) throw fail(413, 'body_too_large'); chunks.push(chunk); }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { throw fail(400, 'invalid_json'); }
}
export function createHandler(ctx, settings = {}) {
  const config = { ...defaults, ...settings };
  let minuteStart = Date.now(), requests = 0, activeReads = 0;
  return async (req, res) => {
    let activeToken;
    const reply = (status, value) => {
      let payload = boundedJson(value);
      if (activeToken) payload = payload.split(JSON.stringify(activeToken).slice(1, -1)).join('[REDACTED]');
      res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' }); res.end(payload);
    };
    try {
      const stored = await jsonFile(config.tokenPath);
      const token = stored.token;
      if (typeof token !== 'string' || token.length < 16) throw fail(503, 'token_configuration_invalid');
      activeToken = token;
      const supplied = req.headers.authorization;
      const expected = `Bearer ${token}`;
      if (typeof supplied !== 'string' || Buffer.byteLength(supplied) !== Buffer.byteLength(expected) || !timingSafeEqual(Buffer.from(supplied), Buffer.from(expected))) throw fail(401, 'unauthorized');
      if (Date.now() - minuteStart >= 60000) { minuteStart = Date.now(); requests = 0; }
      if (++requests > 60) throw fail(429, 'rate_limited');
      const url = new URL(req.url, 'http://observer.invalid');
      if (req.method === 'GET' && url.pathname === '/health') return reply(200, { ok: true, service: name, interactionsSupported: false });
      if (!['/sessions/runtime', '/sessions/control'].includes(url.pathname)) throw fail(404, 'not_found');
      const isControl = url.pathname === '/sessions/control';
      if (req.method !== (isControl ? 'POST' : 'GET')) throw fail(405, 'method_not_allowed');
      const body = isControl ? await requestBody(req) : Object.fromEntries(url.searchParams);
      const { umo, sessionId: sid } = body;
      if (typeof umo !== 'string' || !umo || umo.length > 1024 || typeof sid !== 'string' || !sid || sid.length > 200) throw fail(400, 'exact_binding_required');
      const verify = async () => { if (boundSession(await jsonFile(config.bindingsPath), umo) !== sid) throw fail(409, 'session_binding_mismatch'); };
      await verify();
      if (!isControl) {
        if (!['0', '1', 'false', 'true', undefined].includes(body.detail)) throw fail(400, 'invalid_detail');
        if (activeReads >= 2) throw fail(429, 'observation_busy');
        activeReads++;
        try {
          const result = await snapshot(ctx, sid, !['0', 'false'].includes(body.detail));
          await verify();
          return reply(200, result);
        } finally { activeReads--; }
      }
      if (body.action !== 'stop') throw fail(422, 'interaction_unsupported');
      const agent = ctx.agents.get(sid);
      if (!agent || agent.id !== sid) throw fail(409, 'agent_not_live');
      await verify();
      if (ctx.agents.get(sid) !== agent) throw fail(409, 'agent_replaced');
      agent.cancel({ kind: 'user' }, { keepInbox: true });
      return reply(200, { ok: true, requested: true, sessionId: sid, action: 'stop', status: 'requested', observedAt: new Date().toISOString(), keepInbox: true });
    } catch (error) { reply(error.status ?? 503, { ok: false, error: error.code ?? 'observation_unavailable' }); }
  };
}
export function apply(ctx, config = {}) {
  console.info('[dsh-runtime-observer] activating');
  if (config.interactive === true) throw new Error('Interactive answering is not implemented; existing consumers retain authority.');
  const settings = { ...defaults, ...config };
  if (!Number.isInteger(settings.port) || settings.port < 1 || settings.port > 65535) throw new Error('Invalid observer port');
  const server = http.createServer(createHandler(ctx, settings));
  server.requestTimeout = 15000;
  server.headersTimeout = 10000;
  server.maxConnections = 16;
  server.on('error', error => ctx.logger?.(name)?.error?.('Observer HTTP server error: %s', error.code ?? 'unknown'));
  ctx.effect(() => {
    server.listen(settings.port, settings.host);
    return () => new Promise(resolve => { server.close(resolve); server.closeIdleConnections?.(); });
  });
}
