"""Offline mock regressions. Requires Node.js (NODE_BINARY can give its path)."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('patcher', Path(__file__).with_name('patch_ingress_release.py'))
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)

JS = r'''
import assert from 'node:assert/strict';
let live, phase, closing, resolveDispose, persisted=true, failDispose=false;
const state={sessions:{u:{sessionId:'s',updatedAt:1}},restoring:new Map(),turns:new Map(),pendingApprovals:new Map(),pendingQuestions:new Map()};
const listeners={};
const ctx={on:(n,f)=>{listeners[n]=f},agents:{get:()=>live},sessions:{get:()=>live?.session}};
const logger={warn:()=>{}};
const stopHeartbeat=()=>{};
const isPersisted=async()=>persisted;
__HELPERS__
function setup(){
 phase='idle';closing=false;failDispose=false;persisted=true;
 ingressRequests=0;releaseInFlight=false;
 state.sessions.u={sessionId:'s',updatedAt:1};
 for(const k of ['restoring','turns','pendingApprovals','pendingQuestions'])state[k].clear();
 live={session:{id:'s'},status:'idle',inbox:{nextTurn:[],nextStep:[]},runMaintenance(fn){
   if(phase!=='idle')throw Error('busy');phase='maintenance';
   return Promise.resolve(fn()).finally(()=>{phase='idle';resolveDispose?.();});
 }};
 const handle={agent:live,dispose(){closing=true;return new Promise((resolve,reject)=>{
   resolveDispose=()=>{if(failDispose){reject(Error('failure'));return;}live=undefined;resolve();};
 });}};
 retainReleaseHandle(handle);releaseActivity.set('s',1);return handle;
}
const request={umo:'u',sessionId:'s',idle_seconds:1};
setup();assert.equal((await releaseSession({...request,sessionId:'wrong'}))[0],409);
setup();assert.equal((await releaseSession({...request,idle_seconds:-1}))[0],400);
for(const k of ['restoring','turns','pendingApprovals','pendingQuestions']){
 setup();state[k].set('s',{});assert.equal((await releaseSession(request))[0],409,k);assert.equal(closing,false);
}
setup();live.status='running';assert.equal((await releaseSession(request))[0],409);
setup();live.inbox.nextStep.push({});assert.equal((await releaseSession(request))[0],409);
setup();live.inbox.nextTurn.push({});assert.equal((await releaseSession(request))[0],409);
setup();phase='maintenance';assert.equal((await releaseSession(request))[0],409);assert.equal(closing,false);
setup();releaseHandles.clear();assert.equal((await releaseSession(request))[0],409);
setup();persisted=false;assert.equal((await releaseSession(request))[0],409);assert.equal(closing,false);
setup();ingressRequests=1;assert.equal((await releaseSession(request))[0],409);
setup();listeners['session/event'](live.session);assert.equal((await releaseSession(request))[0],409);
setup();listeners['agent/status']({agent:live});assert.equal((await releaseSession(request))[0],409);
setup();const promise=releaseSession(request);assert.equal(releaseInFlight,true);
assert.equal((await releaseSession(request))[0],409);
const success=await promise;assert.equal(success[0],200);assert.equal(success[1].released,true);
assert.equal(state.sessions.u.sessionId,'s');assert.equal(live,undefined);assert.equal(persisted,true);assert.equal(releaseHandles.size,0);
assert.equal((await releaseSession(request))[1].alreadyReleased,true);
setup();failDispose=true;assert.equal((await releaseSession(request))[0],500);assert.equal(state.sessions.u.sessionId,'s');assert.equal(releaseInFlight,false);
console.log('PASS: 20 release safety scenarios (identity, busy, maintenance, pending work, ownership, durability, gate, activity, dispose, binding, idempotency, failure)');
'''

class ReleaseTests(unittest.TestCase):
    def test_mock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.mjs'
            path.write_text(JS.replace('__HELPERS__', patcher.HELPERS), encoding='utf-8')
            subprocess.run([os.environ.get('NODE_BINARY', 'node'), str(path)], check=True)

    def test_unknown_layout_rejected(self):
        with self.assertRaises(ValueError):
            patcher.patch('export default {}')

    def test_duplicate_patch_rejected(self):
        with self.assertRaises(ValueError):
            patcher.patch('// session-release-v1')

if __name__ == '__main__':
    unittest.main()
