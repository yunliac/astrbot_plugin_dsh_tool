"""Generate an opt-in ingress release patch, never change the input or a running service.

Usage: python tools/patch_ingress_release.py INPUT_INDEX_JS NEW_OUTPUT_JS
Only supports the inspected ingress layout; anchor mismatch aborts without output.
Deploy manually during a maintenance window. Existing live agents are not adopted.
Verified against installed @deepseek-ai/dsh-agent-loop 0.2.1-rc.2 only:
release relies on dispose() synchronously canceling before its first await and
runMaintenance() synchronously claiming idle. Re-audit these semantics on upgrade.
The generated file retains the input's existing copyright/license notices.
"""
from pathlib import Path
import argparse

HELPERS = r'''
  // session-release-v1: only factory handles owned by this ingress are releasable.
  const releaseHandles = new Map()
  const releaseActivity = new Map()
  ctx.on('session/event', (session) => {
    if (releaseHandles.has(session?.id)) releaseActivity.set(session.id, Date.now())
  })
  ctx.on('agent/status', ({ agent }) => {
    if (releaseHandles.has(agent?.session?.id)) releaseActivity.set(agent.session.id, Date.now())
  })
  let releaseInFlight = false
  let ingressRequests = 0
  function retainReleaseHandle(handle) {
    const agent = handle?.agent
    if (agent?.session?.id && typeof handle.dispose === 'function') {
      releaseHandles.set(agent.session.id, handle)
      releaseActivity.set(agent.session.id, Date.now())
    }
    return handle?.agent ?? handle
  }
  function releaseBusy(sessionId, agent) {
    return state.restoring.has(sessionId) || state.turns.has(sessionId)
      || state.pendingApprovals.has(sessionId) || state.pendingQuestions.has(sessionId)
      || agent?.status !== 'idle'
      || !Array.isArray(agent?.inbox?.nextTurn) || agent.inbox.nextTurn.length !== 0
      || !Array.isArray(agent?.inbox?.nextStep) || agent.inbox.nextStep.length !== 0
  }
  async function releaseSession(body) {
    const umo = body?.umo
    const sessionId = body?.sessionId // expected identity; never infer the release target
    const idleSeconds = body?.idle_seconds
    if (typeof umo !== 'string' || !umo || typeof sessionId !== 'string' || !sessionId
        || typeof idleSeconds !== 'number' || !Number.isFinite(idleSeconds) || idleSeconds < 1) {
      return [400, { ok: false, error: 'invalid_release_request' }]
    }
    if (releaseInFlight || ingressRequests) return [409, { ok: false, error: 'ingress_busy' }]
    const mapped = state.sessions[umo]
    if (mapped?.sessionId !== sessionId) return [409, { ok: false, error: 'session_mismatch' }]
    const updatedAt = Math.max(Number(mapped.updatedAt), releaseActivity.get(sessionId) || 0)
    if (!Number.isFinite(updatedAt) || Date.now() - updatedAt < idleSeconds * 1000) {
      return [409, { ok: false, error: 'not_idle_long_enough' }]
    }
    const agent = ctx.agents.get?.(sessionId)
    if (!agent) {
      if (state.restoring.has(sessionId) || state.turns.has(sessionId)
          || state.pendingApprovals.has(sessionId) || state.pendingQuestions.has(sessionId)) {
        return [409, { ok: false, error: 'session_busy' }]
      }
      // Already cold is idempotent, but no assertion that a nonexistent log is durable.
      if (!(await isPersisted(sessionId))) return [409, { ok: false, error: 'not_persisted' }]
      if (state.sessions[umo] !== mapped || ctx.agents.get?.(sessionId) || ingressRequests || releaseInFlight) {
        return [409, { ok: false, error: 'session_changed' }]
      }
      return [200, { ok: true, released: true, alreadyReleased: true, sessionId, bound: true }]
    }
    const handle = releaseHandles.get(sessionId)
    if (!handle || handle.agent !== agent) return [409, { ok: false, error: 'agent_not_owned' }]
    if (typeof agent.runMaintenance !== 'function') return [501, { ok: false, error: 'release_unsupported' }]
    if (releaseBusy(sessionId, agent)) return [409, { ok: false, error: 'session_busy' }]
    // A global ingress gate is deliberately conservative: even requests for other lanes wait.
    releaseInFlight = true
    try {
      if (!(await isPersisted(sessionId))) return [409, { ok: false, error: 'not_persisted' }]
      if (state.sessions[umo] !== mapped || mapped.sessionId !== sessionId
          || ctx.agents.get?.(sessionId) !== agent || releaseBusy(sessionId, agent)) {
        return [409, { ok: false, error: 'session_changed' }]
      }
      if (Date.now() - Math.max(Number(mapped.updatedAt), releaseActivity.get(sessionId) || 0) < idleSeconds * 1000) {
        return [409, { ok: false, error: 'not_idle_long_enough' }]
      }
      let disposal
      let claim
      try {
        // runMaintenance synchronously claims TRUE idle (status alone misses maintenance).
        // Actual installed AgentHandle.dispose enters cancel(disposed) synchronously.
        // Start teardown under the claim, but NEVER await it inside maintenance:
        // dispose waits whenIdle, which would otherwise deadlock on this callback.
        claim = agent.runMaintenance(() => {
          disposal = Promise.resolve(handle.dispose())
          disposal.catch(() => {}) // attach immediately; await outside the maintenance claim
          return Promise.resolve()
        })
      } catch {
        return [409, { ok: false, error: 'agent_has_active_work' }]
      }
      await claim
      await disposal
      if (ctx.agents.get?.(sessionId) || ctx.sessions?.get?.(sessionId)) {
        return [500, { ok: false, error: 'release_incomplete' }]
      }
      if (releaseHandles.get(sessionId) === handle) releaseHandles.delete(sessionId)
      releaseActivity.delete(sessionId)
      stopHeartbeat(sessionId)
      // Keep state.sessions and the persistence log: next inbound resumes this exact id.
      return [200, { ok: true, released: true, sessionId, bound: true }]
    } catch (err) {
      logger.warn?.('session release failed: %s', err?.message ?? err)
      return [500, { ok: false, error: 'release_failed' }]
    } finally {
      releaseInFlight = false
    }
  }
'''
ROUTE = r'''
      if (req.method === 'POST' && url.pathname === '/session/release') {
        if (bearerOf(req) !== state.token) {
          json(res, 401, { ok: false, error: 'unauthorized' })
          return
        }
        let body
        try { body = JSON.parse(await readBody(req, 4096)) } catch {
          json(res, 400, { ok: false, error: 'invalid_json' })
          return
        }
        const [status, result] = await releaseSession(body)
        json(res, status, result)
        return
      }
      // Track the complete async request setup, not SSE lifetime (turns cover that).
      if (releaseInFlight) {
        json(res, 409, { ok: false, error: 'release_in_progress' })
        return
      }
      ingressRequests += 1
      try {
'''

def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f'Expected exactly one anchor: {old[:90]!r}')
    return source.replace(old, new, 1)


def patch(source):
    if 'session-release-v1' in source:
        raise ValueError('Input already patched')
    source = replace_once(source, "const CAPABILITIES = [", "const CAPABILITIES = ['session-release-v1', ")
    source = replace_once(source, '  function ownsAgent(agent, sessionId) {', HELPERS + '\n  function ownsAgent(agent, sessionId) {')
    # Three factory paths in the inspected source. Never adopt agents.get() handles.
    if source.count('return handle?.agent ?? handle') != 3:  # includes new helper itself
        raise ValueError('Unexpected factory return layout')
    # Only replace returns preceding helper, leaving retainReleaseHandle unchanged.
    before, after = source.split('  // session-release-v1:', 1)
    before = before.replace('return handle?.agent ?? handle', 'return retainReleaseHandle(handle)')
    before = replace_once(before, 'const agent = handle?.agent ?? handle', 'const agent = retainReleaseHandle(handle)')
    source = before + '  // session-release-v1:' + after
    source = replace_once(source, "      if (req.method === 'GET' && url.pathname === '/follow') {", ROUTE + "      if (req.method === 'GET' && url.pathname === '/follow') {")
    source = replace_once(source, "      json(res, 404, { ok: false, error: 'not found' })\n    } catch (err) {", "      json(res, 404, { ok: false, error: 'not found' })\n      } finally { ingressRequests -= 1 }\n    } catch (err) {")
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error('Output must differ from input')
    result = patch(args.input.read_text(encoding='utf-8'))
    # Exclusive create: never overwrite a source file or an existing generated patch.
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(result)
    print(f'Generated {args.output}; no service was changed or restarted.')


if __name__ == '__main__':
    main()
