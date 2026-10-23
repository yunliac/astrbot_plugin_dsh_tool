"""Generate authenticated, read-only managed-session APIs on session-release-v1.

Usage: python tools/patch_ingress_sessions.py INPUT_INDEX_JS NEW_OUTPUT_JS
Inspected installed ingress layout only; exclusive output, no deployment/restart.
See ingress_sessions.md for the protocol, limits and verified DSH API contracts.
"""
from pathlib import Path
import argparse

HELPERS = r'''
  // managed-sessions-v1: observations only; never retain/wake agents or touch release activity.
  const sessionReadBudget = { start: 0, count: 0, active: 0 }
  function managedText(value, maxBytes = 16384) {
    if (typeof value !== 'string') return ''
    let text = value.slice(0, maxBytes * 2)
    // Defense in depth for credentials accidentally quoted in conversational text.
    if (state.token) text = text.split(state.token).join('[REDACTED]')
    text = text.replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+=*/gi, 'Bearer [REDACTED]')
      .replace(/\bsk-[A-Za-z0-9_-]{8,}/g, '[REDACTED]')
      .replace(/([^\n]*\b(?:api[_-]?key|access[_-]?token|authorization|password|secret|[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN))\b\s*[=:][^\n]*)/gi, '[REDACTED credential line]')
    const bytes = Buffer.from(text)
    return bytes.length <= maxBytes ? text : bytes.subarray(0, maxBytes).toString('utf8').replace(/\uFFFD$/, '')
  }
  function managedBinding(umo, sessionId) {
    if (typeof umo !== 'string' || !umo.startsWith('tool:') || umo.length <= 5 || umo.length > 1024
        || typeof sessionId !== 'string' || !sessionId || sessionId.length > 256) {
      return [400, { ok: false, error: 'invalid_session_request' }]
    }
    if (!Object.hasOwn(state.sessions, umo) || state.sessions[umo]?.sessionId !== sessionId) {
      return [409, { ok: false, error: 'session_mismatch' }]
    }
    return null
  }
  function managedRow(umo, mapped) {
    const sessionId = mapped.sessionId
    const agent = ctx.agents.get?.(sessionId)
    const liveSession = ctx.sessions?.get?.(sessionId) ?? agent?.session
    const waitingApproval = state.pendingApprovals.has(sessionId)
    const waitingQuestion = state.pendingQuestions.has(sessionId)
    const waiting = waitingApproval || waitingQuestion || Boolean(state.turns.get(sessionId)?.awaitingHuman)
    const running = agent?.status === 'running'
    const live = Boolean(liveSession || agent)
    const status = waitingApproval ? 'waiting_approval' : waitingQuestion ? 'waiting_question'
      : waiting ? 'unknown' : running ? 'running' : agent?.status === 'idle' ? 'idle' : !live ? 'cold' : 'unknown'
    // Cache title only, never the goal/config/environment or a full log read for listing.
    let title = liveSession?.title || foldTitle(liveEventsOf(liveSession) ?? [])
    if (!title) {
      try { title = loadCacheRecord(sessionId)?.rows?.title?.val } catch { /* unavailable */ }
    }
    const updatedAt = Number.isFinite(Number(mapped.updatedAt)) ? Number(mapped.updatedAt) : null
    const activity = releaseActivity.get(sessionId) || 0
    return { umo, sessionId, sid: sessionId, cwd: managedText(mapped.cwd, 4096) || null,
      title: managedText(title, 1024) || '新会话', updatedAt,
      lastActivity: Math.max(updatedAt || 0, activity) || null,
      status, live, running, waiting }
  }
  function managedSessions() {
    const sessions = []
    let truncated = false
    for (const [umo, mapped] of Object.entries(state.sessions)) {
      if (!umo.startsWith('tool:') || umo.length <= 5 || umo.length > 1024
          || typeof mapped?.sessionId !== 'string' || !mapped.sessionId || mapped.sessionId.length > 256) continue
      if (sessions.length >= 200) { truncated = true; break }
      sessions.push(managedRow(umo, mapped))
    }
    return { ok: true, sessions, truncated }
  }
  async function managedHistory(umo, sessionId, rawLimit) {
    const invalid = managedBinding(umo, sessionId)
    if (invalid) return invalid
    const limit = rawLimit === null || rawLimit === undefined ? 100 : Number(rawLimit)
    if (!Number.isInteger(limit) || limit < 1 || limit > 200) {
      return [400, { ok: false, error: 'invalid_limit' }]
    }
    const now = Date.now()
    if (now - sessionReadBudget.start >= 60000) {
      sessionReadBudget.start = now; sessionReadBudget.count = 0
    }
    if (sessionReadBudget.active >= 2 || sessionReadBudget.count >= 30) {
      return [429, { ok: false, error: 'history_rate_limited', retryAfter: 60 }]
    }
    sessionReadBudget.count += 1
    sessionReadBudget.active += 1
    const mapped = state.sessions[umo]
    try {
      // Reject oversized/unknown cold logs BEFORE the full-history helper reads them.
      // stat/open('read')/inspect are observations, not agent resume operations.
      const live = ctx.sessions?.get?.(sessionId) ?? ctx.agents.get?.(sessionId)?.session
      if (!live) {
        let snapshot
        try { snapshot = await ctx.sessionPersistence?.stat?.(sessionId) } catch {
          return [503, { ok: false, error: 'history_unavailable' }]
        }
        if (!snapshot) return [404, { ok: false, error: 'session_not_found' }]
        if (!Number.isFinite(snapshot.sizeBytes) || snapshot.sizeBytes < 0) {
          return [503, { ok: false, error: 'history_size_unknown' }]
        }
        if (snapshot.sizeBytes > 8 * 1024 * 1024) return [413, { ok: false, error: 'history_too_large' }]
      }
      const events = await readSessionEvents(sessionId, live)
      // Binding may change while a persistence read is pending. Never return stale ownership.
      if (state.sessions[umo] !== mapped || managedBinding(umo, sessionId)) {
        return [409, { ok: false, error: 'session_mismatch' }]
      }
      if (!Array.isArray(events)) return [503, { ok: false, error: 'history_unavailable' }]
      const messages = []
      let bytes = 0
      let truncated = events.length > 20000
      let stopped = -1
      const floor = Math.max(0, events.length - 20000)
      for (let i = events.length - 1; i >= floor; i--) {
        const event = events[i]
        if (event?.type !== 'user/message' && event?.type !== 'assistant/message') continue
        const role = event.type === 'user/message' ? 'user' : 'assistant'
        const message = role === 'user' ? event.data : event.data?.message
        const raw = message?.content
        // Allowlist plain text blocks only: never tools, reasoning, requests, env or attachments.
        const text = typeof raw === 'string' ? raw : Array.isArray(raw)
          ? raw.filter((part) => part?.type === 'text' && typeof part.text === 'string')
            .map((part) => part.text.slice(0, 32768)).join('\n') : ''
        if (!text.trim()) continue
        const content = managedText(text)
        const row = { role, content }
        if (typeof event.ts === 'number' && Number.isFinite(event.ts)) row.timestamp = event.ts
        const size = Buffer.byteLength(JSON.stringify(row))
        if (messages.length >= limit || bytes + size > 256 * 1024) { stopped = i; truncated = true; break }
        if (Buffer.byteLength(text) > 16384) truncated = true
        messages.push(row); bytes += size
      }
      messages.reverse()
      return [200, { ok: true, sessionId, messages, truncated }]
    } catch {
      // No backend exception text: it can contain paths, env, provider credentials.
      return [503, { ok: false, error: 'history_unavailable' }]
    } finally { sessionReadBudget.active -= 1 }
  }
'''

ROUTE = r'''
      // Both reads intentionally precede the release gate and do NOT increment ingressRequests.
      if (req.method === 'GET' && (url.pathname === '/sessions/managed' || url.pathname === '/sessions/history')) {
        res.setHeader('Cache-Control', 'no-store')
        if (bearerOf(req) !== state.token) {
          json(res, 401, { ok: false, error: 'unauthorized' })
          return
        }
        if (url.pathname === '/sessions/managed') {
          json(res, 200, managedSessions())
        } else {
          const [status, result] = await managedHistory(url.searchParams.get('umo'), url.searchParams.get('sessionId'), url.searchParams.get('limit'))
          if (status === 429) res.setHeader('Retry-After', '60')
          json(res, status, result)
        }
        return
      }
'''


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f'Expected exactly one anchor: {old[:90]!r}')
    return source.replace(old, new, 1)


def patch(source):
    if 'managed-sessions-v1' in source:
        raise ValueError('Input already contains managed-sessions-v1')
    for anchor in ['const releaseActivity = new Map()', 'let releaseInFlight = false',
                   'let ingressRequests = 0', 'async function readSessionEvents(',
                   'async function listWorkspaceSessions(', 'function foldTitle(',
                   'function loadCacheRecord(']:
        if source.count(anchor) != 1:
            raise ValueError(f'Missing inspected release/API anchor: {anchor}')
    source = replace_once(source, "const CAPABILITIES = ['session-release-v1', ",
                          "const CAPABILITIES = ['managed-sessions-v1', 'session-release-v1', ")
    source = replace_once(source, '  function ownsAgent(agent, sessionId) {',
                          HELPERS + '\n  function ownsAgent(agent, sessionId) {')
    gate = '      // Track the complete async request setup, not SSE lifetime (turns cover that).\n      if (releaseInFlight) {'
    source = replace_once(source, gate, ROUTE + gate)
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error('Output must differ from input')
    result = patch(args.input.read_text(encoding='utf-8'))
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(result)
    print(f'Generated {args.output}; no service was changed or restarted.')


if __name__ == '__main__':
    main()
