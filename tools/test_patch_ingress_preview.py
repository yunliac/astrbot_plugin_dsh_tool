"""Isolated regression test for read-only pagination over real helper JavaScript."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from patch_ingress_preview import HELPER

class PreviewTests(unittest.TestCase):
    def test_all_messages_and_long_unicode_content(self):
        script = r'''
const assert=require('node:assert/strict');
const token='private-test-secret';
const state={token,sessions:{'tool:test':{sessionId:'session-test'}}};
const sessionReadBudget={start:Date.now(),count:0,active:0};
const ctx={sessions:{get:()=>({})}};
function managedBinding(u,s){return u==='tool:test'&&s==='session-test'?null:[409,{ok:false}]}
function managedText(t,max){return t.split(token).join('[REDACTED]')}
const big='中文😀'.repeat(20000);
const events=[{type:'user/message',data:{content:big},ts:1},...Array.from({length:125},(_,i)=>({type:'assistant/message',data:{message:{content:'answer '+i}}}))];
async function readSessionEvents(){return events}
''' + HELPER + r'''
(async()=>{
 let cursor='', rows=[], iterations=0;
 do {
   const [status,data]=await managedPreview('tool:test','session-test',cursor);
   assert.equal(status,200);rows.push(...data.messages);cursor=data.next_cursor;
   assert.ok(++iterations<20);
 }while(cursor);
 assert.equal(rows.filter(r=>r.message_id===0).map(r=>r.content).join(''),big);
 assert.equal(rows.filter(r=>r.role==='assistant').length,125);
 assert.ok(rows.every(r=>!r.content.includes('\ufffd')));
 assert.equal((await managedPreview('tool:test','bad',''))[0],409);
 assert.equal((await managedPreview('tool:test','session-test','bad'))[0],400);
 console.log('PASS all 126 messages, long unicode chunks, binding and cursor validation');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'test.cjs'
            path.write_text(script, encoding='utf-8')
            subprocess.run([os.environ.get('NODE_BINARY', 'node'), str(path)], check=True)

if __name__ == '__main__':
    unittest.main()
