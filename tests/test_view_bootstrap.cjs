// Simulate HTML parser execution order without browser packages.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(require('node:path').join(__dirname, '../views/dsh/index.html'), 'utf8');
class Element {
  constructor() { this.value = ''; this.checked = true; this.classList = {toggle(){},add(){}}; this.children=[]; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener() {}
  setAttribute() {}
}
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id,new Element()); return elements.get(id); };
get('status').value='all';
const requests=[];
const window={parent:{},addEventListener(){}};
const document={getElementById:get,createElement:()=>new Element(),createTextNode:text=>({textContent:text}),addEventListener(){},hidden:false};
const context=vm.createContext({window,document,console,setTimeout,clearTimeout,setInterval:()=>0,clearInterval(){},URL,Blob});
for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
  if (/src=/.test(match[1])) {
    assert.match(match[1],/\/api\/plugin\/page\/bridge-sdk\.js/);
    window.AstrBotPluginView={ready:async()=>({}),apiPost:async(endpoint)=>{
      requests.push(endpoint);
      return {sessions:[{id:'test',session_id:'session-test',title:'existing conversation',status:'cold'}],ingress_available:true,idle:{enabled:true,timeout_sec:1800}};
    }};
  } else vm.runInContext(match[2],context);
}
setImmediate(()=>{
  assert.deepEqual(requests,['sessions/refresh'],'page must initiate first session refresh after SDK loads');
  assert.equal(get('total').textContent,1);
  assert.equal(get('rows').children.length,1);
  console.log('PASS parser-order bootstrap sends refresh and renders existing conversation');
});
