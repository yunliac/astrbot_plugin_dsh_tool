import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, mkdtemp, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { Readable } from 'node:stream';
// No dependency installation or ingress import: schema stub only for isolated business mocks.
const source = (await readFile(new URL('./dsh_runtime_observer.mjs', import.meta.url), 'utf8')).replace("import z from '@deepseek-ai/schemastery';", "const field = () => ({ default() { return this; } }); const z = {object: x => x, string: field, number: field};");
const { snapshot, createHandler, clean, boundSession } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
function mock() {
  let disposed = 0, cancelled;
  const agent = { id: 'sid', status: 'running', cancel(...args) { cancelled = args; } };
  const ctx = {
    agents: { get: () => agent }, sessionPersistence: { stat() { throw new Error('live must not stat'); } },
    sessionQuery: { async observeSession(sid, options) {
      assert.equal(sid, 'sid'); assert.equal(options.projectionMode, 'all');
      return { header: { id: sid }, source: 'live', cursor: 3, projections: { values: { todos: [], goal: null } },
        events: [ { type: 'user/message', seq: 0, time: 10, data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'hello' }, { type: 'reasoning', text: 'PRIVATE' }] } },
          { type: 'tool/call', seq: 1, time: 11, data: { callId: 'c1', name: 'test', arguments: 'Bearer ABC' } },
          { type: 'tool/result', seq: 2, time: 12, data: { message: { toolCallId: 'c1', content: [{ type: 'text', text: 'ok' }] }, meta: { secret: 'NEVER' } } } ],
        [Symbol.dispose]() { disposed++; } };
    } }, jobs: { list: () => [{ id: 'ours', owner: 'sid', output: { earliest: 0, total: 10000 } }, { id: 'unowned' }, { id: 'foreign', owner: 'other' }],
      readAt(id, from, sid) { assert.equal(id, 'ours'); assert.equal(from, 1808); assert.equal(sid, 'sid'); return { next: 10000, lossy: false, chunks: [{ at: 1808, text: 'tail' }] }; },
      read() { throw new Error('consuming read forbidden'); }, wait() { throw new Error('wait forbidden'); } },
    terminals: { list: () => [{ sessionId: 'pty', status: { kind: 'running' } }], read(owner, id, request) { assert.equal(owner, agent); assert.deepEqual(request, { offset: 0, count: 40 }); return { text: 'terminal', totalLines: 1, lineBegin: 0, lineEnd: 1, truncated: false }; } },
  };
  return { ctx, agent, disposed: () => disposed, cancelled: () => cancelled };
}
test('bounded evidence, proper callId, nonconsuming jobs and lease disposal', async () => {
  const m = mock(), out = await snapshot(m.ctx, 'sid');
  assert.equal(m.disposed(), 1); assert.equal(out.jobs.length, 1);
  assert.equal(out.evidence.tools[1].callId, 'c1'); assert.equal(out.evidence.tools[0].time, 11);
  assert.equal(out.evidence.messages[0].text, 'hello');
  assert.doesNotMatch(JSON.stringify(out), /PRIVATE|NEVER|Bearer ABC|spill/);
});
test('missing optional jobs terminals and goals do not block snapshots', async () => {
  const m = mock(); delete m.ctx.jobs; delete m.ctx.terminals;
  m.ctx.get = key => m.ctx[key];
  const out = await snapshot(m.ctx, 'sid');
  assert.equal(out.ok, true); assert.deepEqual(out.capabilities, { jobs: false, terminals: false, goals: false });
  assert.deepEqual(out.jobs, []); assert.deepEqual(out.terminals, []);
  assert.ok(out.uncertain.some(text => text.includes('Jobs capability unavailable')));
  assert.equal(m.disposed(), 1);
});
test('lease disposed on evidence exception', async () => {
  const m = mock(), original = m.ctx.sessionQuery.observeSession;
  m.ctx.sessionQuery.observeSession = async (...args) => { const lease = await original(...args); Object.defineProperty(lease, 'events', { get() { throw new Error('broken'); } }); return lease; };
  await assert.rejects(snapshot(m.ctx, 'sid'), /broken/); assert.equal(m.disposed(), 1);
});
test('cold oversized and unknown sizes rejected before observing', async () => {
  const m = mock(); m.ctx.agents.get = () => undefined;
  for (const sizeBytes of [9 * 1024 * 1024, undefined]) {
    m.ctx.sessionPersistence.stat = async () => ({ sizeBytes });
    await assert.rejects(snapshot(m.ctx, 'sid'), error => error.status === 413);
  }
  assert.equal(m.disposed(), 0);
});
test('binding parsing and redaction fail closed', () => {
  assert.equal(boundSession({ 'tool:u': { sessionId: 'sid' } }, 'u'), 'sid');
  assert.equal(boundSession({ 'tool:u': { sessionId: 'sid' } }, 'tool:u'), 'sid');
  assert.equal(boundSession({ u: { sessionId: 'sid' } }, 'u'), undefined);
  assert.equal(boundSession({}, '__proto__'), undefined);
  assert.doesNotMatch(clean('\x1b[31mAPI_KEY=abc\nBearer abc\nsk-abcdef'), /abc/);
});
async function invoke(handler, method, url, data, authorization = 'Bearer 1234567890123456') {
  const req = Readable.from(data ? [Buffer.from(JSON.stringify(data))] : []);
  Object.assign(req, { method, url, headers: { authorization } });
  let status, payload;
  await handler(req, { writeHead(s) { status = s; }, end(p) { payload = JSON.parse(p); } });
  return { status, payload };
}
test('HTTP auth, exact binding, stop only and unsupported approval', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'observer-test-'));
  try {
    const tokenPath = path.join(dir, 'config.json'), bindingsPath = path.join(dir, 'sessions.json');
    await writeFile(tokenPath, JSON.stringify({ token: '1234567890123456' }));
    await writeFile(bindingsPath, JSON.stringify({ 'tool:u': { sessionId: 'sid' } }));
    const m = mock(), handler = createHandler(m.ctx, { tokenPath, bindingsPath });
    const unauthorized = await invoke(handler, 'GET', '/health', undefined, 'bad');
    assert.equal(unauthorized.status, 401); assert.equal(unauthorized.payload.ok, false);
    assert.equal((await invoke(handler, 'GET', '/sessions/runtime?umo=u&sessionId=wrong')).status, 409);
    assert.equal((await invoke(handler, 'POST', '/sessions/control', { umo: 'u', sessionId: 'sid', action: 'approve' })).status, 422);
    const stopped = await invoke(handler, 'POST', '/sessions/control', { umo: 'u', sessionId: 'sid', action: 'stop' });
    assert.equal(stopped.payload.status, 'requested'); assert.equal(stopped.payload.ok, true); assert.equal(stopped.payload.requested, true); assert.deepEqual(m.cancelled(), [{ kind: 'user' }, { keepInbox: true }]);
    const result = await invoke(handler, 'GET', '/sessions/runtime?umo=u&sessionId=sid');
    assert.equal(result.status, 200); assert.equal(result.payload.ok, true); assert.equal(result.payload.interactionsSupported, false);
    assert.equal((await invoke(handler, 'GET', '/sessions/runtime?umo=tool%3Au&sessionId=sid&detail=0')).payload.ok, true);
    assert.ok(Buffer.byteLength(JSON.stringify(result.payload)) <= 128 * 1024);
    const originalObserve = m.ctx.sessionQuery.observeSession;
    m.ctx.sessionQuery.observeSession = async (...args) => {
      const lease = await originalObserve(...args);
      lease.projections.values.todos = Array.from({ length: 100 }, () => ({ content: '界'.repeat(1000), status: 'pending' }));
      return lease;
    };
    const oversized = await invoke(handler, 'GET', '/sessions/runtime?umo=u&sessionId=sid');
    assert.equal(oversized.payload.truncated, true);
    assert.ok(Buffer.byteLength(JSON.stringify(oversized.payload)) <= 128 * 1024);
    assert.equal(oversized.payload.ok, true);
    const limited = createHandler(m.ctx, { tokenPath, bindingsPath });
    for (let i = 0; i < 60; i++) assert.equal((await invoke(limited, 'GET', '/health')).status, 200);
    assert.equal((await invoke(limited, 'GET', '/health')).status, 429);
    let release, entered = 0, bothEntered;
    const ready = new Promise(resolve => { bothEntered = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    m.ctx.sessionQuery.observeSession = async (...args) => { if (++entered === 2) bothEntered(); await gate; return originalObserve(...args); };
    const concurrent = createHandler(m.ctx, { tokenPath, bindingsPath });
    const first = invoke(concurrent, 'GET', '/sessions/runtime?umo=u&sessionId=sid');
    const second = invoke(concurrent, 'GET', '/sessions/runtime?umo=u&sessionId=sid');
    await ready;
    const third = await invoke(concurrent, 'GET', '/sessions/runtime?umo=u&sessionId=sid');
    assert.equal(third.status, 429);
    release();
    assert.equal((await first).status, 200); assert.equal((await second).status, 200);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
