"""Add read-only paginated conversation preview to managed-sessions-v1 ingress."""
from pathlib import Path
import argparse

HELPER = r'''
  // conversation-preview-v1: forward event + character cursor, no agent resume.
  async function managedPreview(umo, sessionId, rawCursor) {
    const invalid = managedBinding(umo, sessionId)
    if (invalid) return invalid
    const match = /^(\d{1,10}):(\d{1,10})$/.exec(rawCursor || '0:0')
    if (!match) return [400, {ok:false,error:'invalid_cursor'}]
    let index = Number(match[1]), offset = Number(match[2])
    const now = Date.now()
    if (now-sessionReadBudget.start >= 60000) {sessionReadBudget.start=now;sessionReadBudget.count=0}
    if (sessionReadBudget.active >= 2 || sessionReadBudget.count >= 60)
      return [429,{ok:false,error:'history_rate_limited'}]
    sessionReadBudget.active++; sessionReadBudget.count++
    const mapped = state.sessions[umo]
    try {
      const live = ctx.sessions?.get?.(sessionId) ?? ctx.agents.get?.(sessionId)?.session
      if (!live) {
        const snapshot = await ctx.sessionPersistence?.stat?.(sessionId)
        if (!snapshot || !Number.isFinite(snapshot.sizeBytes)) return [503,{ok:false,error:'history_unavailable'}]
        if (snapshot.sizeBytes > 8*1024*1024) return [413,{ok:false,error:'history_too_large'}]
      }
      const events = await readSessionEvents(sessionId, live)
      if (state.sessions[umo] !== mapped || managedBinding(umo,sessionId)) return [409,{ok:false,error:'session_mismatch'}]
      if (!Array.isArray(events)) return [503,{ok:false,error:'history_unavailable'}]
      if (index > events.length) return [409,{ok:false,error:'history_changed'}]
      const messages=[]
      let scanned=0, bytes=0
      while(index < events.length && messages.length < 50 && scanned++ < 20000) {
        const event=events[index]
        if(event?.type!=='user/message' && event?.type!=='assistant/message') {index++;offset=0;continue}
        const role=event.type==='user/message'?'user':'assistant'
        const raw=(role==='user'?event.data:event.data?.message)?.content
        const text=typeof raw==='string'?raw:Array.isArray(raw)?raw.filter(p=>p?.type==='text'&&typeof p.text==='string').map(p=>p.text).join('\n'):''
        // Redact before splitting so credentials cannot straddle chunk boundaries.
        const safe=managedText(text,Buffer.byteLength(text)+1)
        if(offset > safe.length) return [409,{ok:false,error:'history_changed'}]
        if(!safe.trim() || offset===safe.length) {index++;offset=0;continue}
        let end=Math.min(safe.length,offset+8192)
        if(end<safe.length && /[\uD800-\uDBFF]/.test(safe[end-1])) end--
        const row={role,content:safe.slice(offset,end),message_id:index,continued:offset>0}
        if(typeof event.ts==='number'&&Number.isFinite(event.ts))row.timestamp=event.ts
        const size=Buffer.byteLength(JSON.stringify(row))
        if(bytes+size>256*1024)break
        messages.push(row);bytes+=size
        if(end<safe.length) offset=end
        else {index++;offset=0}
      }
      const complete=index>=events.length
      return [200,{ok:true,sessionId,messages,next_cursor:complete?null:`${index}:${offset}`,complete,
        notice:'仅显示用户与助手正文；不含工具调用、思考过程及附件二进制内容。疑似凭据已脱敏。'}]
    } catch {return [503,{ok:false,error:'history_unavailable'}]}
    finally {sessionReadBudget.active--}
  }
'''
ROUTE = r'''
      if(req.method==='GET' && url.pathname==='/sessions/preview') {
        res.setHeader('Cache-Control','no-store')
        if(bearerOf(req)!==state.token) {json(res,401,{ok:false,error:'unauthorized'});return}
        const [status,result]=await managedPreview(url.searchParams.get('umo'),url.searchParams.get('sessionId'),url.searchParams.get('cursor'))
        json(res,status,result);return
      }
'''
def patch(source):
    if 'conversation-preview-v1' in source:
        raise ValueError('Already patched')
    if 'managed-sessions-v1' not in source:
        raise ValueError('Requires managed-sessions-v1')
    anchor='  async function managedHistory('
    route='      // Both reads intentionally precede the release gate'
    if source.count(anchor)!=1 or source.count(route)!=1:
        raise ValueError('Unrecognized source layout')
    source=source.replace("const CAPABILITIES = ['managed-sessions-v1',", "const CAPABILITIES = ['conversation-preview-v1', 'managed-sessions-v1',", 1)
    source=source.replace(anchor,HELPER+'\n'+anchor,1)
    source=source.replace(route,ROUTE+route,1)
    return source
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('input',type=Path);p.add_argument('output',type=Path)
    a=p.parse_args()
    if a.input.resolve() == a.output.resolve():
        p.error('Output must differ from input')
    result = patch(a.input.read_text(encoding='utf-8'))
    with a.output.open('x',encoding='utf-8') as f:f.write(result)
