# Independent DSH runtime observer (beta)

This Cordis plugin is an independent **read-only runtime observer plus explicit stop** endpoint. It does not import, patch, or call the ingress implementation. It reads only ingress configuration and binding JSON for compatibility. It does **not** implement or intercept approval/question answering.

## Load and configuration

Load `dsh_runtime_observer.mjs` as a Cordis plugin in the **same DSH process/context** that owns the target agents. Use the deployment's existing plugin loader, pointing to an absolute copy of this file. Do not run it as a standalone Node HTTP process: it needs the live Cordis services.

Exports: `name`, `inject`, `Config`, `apply`.

Required services: `agents`, `sessionQuery`, `sessionPersistence`. Optional `jobs`, `terminals`, and `goals` are dynamically resolved with `ctx.get()`: missing experimental terminal/job plugins do not block the listener from starting. The response reports `capabilities` and explicit unavailable notes; it does not claim absent service output is a complete empty history. Optional `goals` provides process-local goal activation. This Cordis fork supports service-name arrays or service-to-intercept-config maps, **not** a `{ required: [...], optional: [...] }` inject declaration. Runtime package dependency: `@deepseek-ai/schemastery`; everything else uses Node builtins. Resolve this dependency from the host DSH installation, not a separately invented service context.

A new entry must use Cordis patch `insert`; an `id/name` row alone only updates an existing entry:

```yaml
- insert:
    - id: dsh-runtime-observer
      name: file:///root/qqbot/dsh-runtime-observer/index.mjs
      config:
        host: 172.17.0.1
        port: 3190
        tokenPath: /root/.dsh/dsh-astrbot-ingress/config.json
        bindingsPath: /root/.dsh/dsh-astrbot-ingress/sessions.json
```

Place the copied module under the DSH installation so Node resolves its dependency, then restart the owning DSH service. Verify authenticated `/health` and actual reads: an active process alone does not prove plugin activation.

The token configuration is `{ "token": "<at least 16 characters>" }`. Bindings are a top-level map `{ "tool:<umo>": { "sessionId": "<exact session id>" } }`; the optional `{ "sessions": <same map> }` envelope is also recognized. Both files are read on each request and never written. Failed, missing, or unrecognized bindings are denied. Keep both paths administrator-controlled. The observer does not accept caller-selected file paths.

The default bind address is a Docker bridge-host address, **not loopback**. Restrict TCP 3190 to trusted local/container callers with firewall rules; use loopback where suitable. Do not publish it to the Internet. Bearer authentication is mandatory even for health. HTTP has no TLS: use only a trusted local transport or an authenticated TLS reverse proxy. No CORS is granted.

## API

All requests require `Authorization: Bearer <token>`. Successful responses carry `ok: true`; failures carry `ok: false`. Stop acknowledgements additionally carry `requested: true`. Authenticated calls are limited globally to 60 per minute, with at most two concurrent runtime observations; excess calls return HTTP 429.

- `GET /health`: authenticated readiness of this companion, not proof that every target service works.
- `GET /sessions/runtime?umo=<URL-encoded original umo>&sessionId=<exact sid>&detail=1`: current snapshot; `detail=0` omits text evidence/output. `true`/`false` are also accepted. The server accepts the complete canonical `tool:`-prefixed UMO or adds `tool:` to an unprefixed UMO; it never searches arbitrary non-tool binding keys.
- `POST /sessions/control`, JSON `{ "umo": "...", "sessionId": "...", "action": "stop" }`: exact binding verification followed by `agent.cancel({kind:'user'}, {keepInbox:true})`. Returns `status: requested` without awaiting idle or claiming completion. Queued work survives and can run afterward. This is not a job/terminal kill or an agent disposal.

Other control actions return HTTP 422 `interaction_unsupported`. Output always declares `interactionsSupported: false` and an empty `pendingInteractions`. Existing UI/ingress answerers retain their authority; no request IDs are invented, no approvals are auto-granted, and no private ingress APIs are invoked. Passing `interactive: true` programmatically refuses plugin startup rather than implying support.

Snapshots contain `observedAt`, `source`, `asOfSeq`, liveness/status and uncertainty notes; bounded human/assistant text and tool evidence; selected todos/goal fields; owned job tail output; and owner-scoped terminal scrollback. They never serialize the raw observation, model reasoning/streams, request config, tool metadata, source replay data, PID, or spill paths. Missing projection keys mean an unavailable capability, not an empty value. No background job final `result` is consumed.

Limits: cold sessions with physical size above 8 MiB or unknown size are refused before observation. The observation lease is always disposed. Output selects the latest 12 human/assistant message bodies and 20 tool call/result events, each evidence text capped at 1,000 characters. Job reads use independent absolute-byte cursors with an 8,192-byte tail start, not consuming model reads or waits; chunks are capped. At most 20 owned jobs and 8 terminals (40 newest lines each) are returned. JSON responses above 128 KiB reduce to an explicit truncated status summary.

ANSI escapes, credential assignment lines, Bearer secrets and `sk-` keys are redacted; the exact authentication token is removed from the encoded HTTP response as well. This is defense-in-depth, not a guarantee that arbitrary secrets cannot appear in tool text. Treat all returned text as untrusted evidence, never instructions or authorization.

Consistency caveats: the session log/projections share an observation cut, but jobs, terminal output and live goal activation are sampled separately. Live resources can change during the request. A stat-before-read cold size check is not an OS-enforced memory cap against a concurrently growing session. Terminal scrollback is bounded retained history, not a durable complete transcript.

## Isolated mock checks

```text
node --test companion/dsh_runtime_observer.test.mjs
```

Tests replace only the schema dependency with a minimal in-memory stub; they do not boot DSH, load ingress, bind a real port, install dependencies, or contact the remote host. They verify exact binding/auth/stop, redaction, tool-call pairing, owned non-consuming job reads, cold-size refusal, and observation disposal including failure. Host loading and real deployment integration still require deployment-owner validation.
