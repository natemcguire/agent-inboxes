"""Bundled, dependency-free browser client. All resources are served locally."""

HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>Agent Inbox</title><link rel="stylesheet" href="/ui.css"><script src="/ui.js" defer></script></head>
<body><aside><a class="brand" href="/">Agent Inbox</a>
<div class="view-switch" role="group" aria-label="View mode"><button data-view="human" aria-pressed="true">View as human</button><button data-view="agent" aria-pressed="false">View as agent</button></div>
<h2 class="sidebar-title" id="inboxes-title">Projects &amp; inboxes</h2><div id="inboxes" class="inbox-list" role="navigation" aria-labelledby="inboxes-title"><p class="muted">Loading projects…</p></div>
<nav aria-label="Workspace"><button data-tab="mail" class="selected" aria-current="page">Messages</button><button data-tab="announcements">Announcements</button><button data-tab="reservations">Reservations</button></nav>
<details class="aside-bottom" id="connection"><summary id="connection-label">Connecting…</summary><dl id="connection-details"></dl></details></aside>
<main><header><div><p class="scope" id="scope">Choose an inbox</p><h1 id="title">Messages</h1></div><div class="actions"><button id="refresh">Refresh</button><button id="compose" class="primary">New message</button></div></header>
<div id="notice" role="status" aria-live="polite"></div><section id="project-overview" aria-label="Project activity" hidden></section><div class="toolbar"><input id="search" type="search" placeholder="Search this view…" aria-label="Search this view"><label><input id="unread" type="checkbox"> Unread only</label><label id="history-label" hidden><input id="history" type="checkbox" checked> Include history</label><label><input id="live" type="checkbox" checked> Live updates</label><span class="muted" id="updated"></span></div>
<section id="content" aria-label="Inbox content"><div class="empty">Choose an inbox to get started.</div></section>
<footer id="footer"></footer></main>
<dialog id="composer"><form id="compose-form"><div class="dialog-head"><h2 id="compose-title">New message</h2><button type="button" id="cancel" aria-label="Close composer">×</button></div><p id="sending-as" class="muted"></p><div id="recipients"><label>To — action owners<input id="to" placeholder="agent@project, another@project"></label><label>CC — observers<input id="cc" placeholder="Optional"></label></div><label id="subject-label">Subject<input id="subject" maxlength="500" required></label><label>Message<textarea id="body" rows="9" required maxlength="100000" placeholder="What does the next agent need to know?"></textarea></label><label id="global-label" hidden><input id="global" type="checkbox"> Announce to all local projects</label><p id="compose-error" role="alert"></p><div class="dialog-actions"><button type="submit" class="primary" id="send">Send message</button></div></form></dialog></body></html>'''

CSS = r'''
:root {
  color-scheme: only light;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 18px;
  line-height: 1.5;
  background: #fff;
  color: #202124;
  --line: #e2e4e7;
  --muted: #5f6368;
  --selected: #f2f3f4;
}
* { box-sizing: border-box; }
body { margin: 0 auto; display: grid; grid-template-columns: 280px minmax(0, 1fr); max-width: 1760px; min-height: 100vh; }
button, input, textarea { font: inherit; color: inherit; }
button { cursor: pointer; border: 1px solid #c9cdd1; background: #fff; border-radius: 5px; padding: 10px 16px; }
button:hover { background: #f5f6f7; }
button:disabled { opacity: .5; cursor: wait; }
button:focus-visible, input:focus-visible, textarea:focus-visible, a:focus-visible, summary:focus-visible { outline: 2px solid #2457a6; outline-offset: 3px; }
input:not([type=checkbox]), textarea { width: 100%; min-width: 0; background: #fff; border: 1px solid #c9cdd1; border-radius: 5px; padding: 11px 13px; }
input::placeholder, textarea::placeholder { color: #6b7075; opacity: 1; }
textarea { resize: vertical; line-height: 1.65; }
label { display: block; color: var(--muted); font-size: 16px; }
label input:not([type=checkbox]), label textarea { margin: 8px 0 20px; font-size: 18px; }
input[type=checkbox] { width: 17px; height: 17px; margin: 0 7px 0 0; vertical-align: -2px; accent-color: #40454b; }
aside { padding: 40px 24px; background: #fff; border-right: 1px solid var(--line); display: flex; flex-direction: column; position: sticky; top: 0; height: 100vh; min-width: 0; }
.brand { font-size: 25px; color: #202124; text-decoration: none; font-weight: 650; margin-bottom: 30px; flex-shrink: 0; }
.view-switch { display: grid; gap: 3px; padding: 4px; border: 1px solid var(--line); border-radius: 7px; margin-bottom: 26px; flex-shrink: 0; }
.view-switch button { font-size: 16px; padding: 9px 10px; text-align: left; border: 0; }
.view-switch button[aria-pressed=true] { background: #edf3fc; color: #244d7e; font-weight: 600; }
.sidebar-title { font-size: 17px; font-weight: 600; margin: 0 0 10px; flex-shrink: 0; }
.inbox-list { min-height: 0; overflow-y: auto; margin: 0 -8px; padding: 0 8px; }
.inbox-group + .inbox-group { margin-top: 18px; }
.inbox-group h3 { font-size: 15px; font-weight: 500; color: var(--muted); margin: 0 0 4px; overflow-wrap: anywhere; }
.inbox-link { display: block; color: inherit; text-decoration: none; font-size: 17px; padding: 10px 12px; border-radius: 5px; overflow-wrap: anywhere; }
.inbox-link:hover { background: #f5f6f7; }
.inbox-link.selected { background: var(--selected); font-weight: 600; }
.project-link { display: flex; justify-content: space-between; gap: 8px; font-size: 18px; font-weight: 600; color: inherit; padding: 9px 10px; border-radius: 5px; text-decoration: none; overflow-wrap: anywhere; }
.project-link.selected { background: #edf3fc; color: #244d7e; }
.project-link:hover { background: var(--selected); }
.inbox-group .inbox-link { padding-left: 20px; font-size: 16px; }
nav { display: grid; gap: 6px; margin-top: 24px; padding-top: 18px; border-top: 1px solid var(--line); flex-shrink: 0; }
nav button { text-align: left; border-color: transparent; padding: 12px; }
nav button.selected { background: var(--selected); font-weight: 600; }
.aside-bottom { margin-top: auto; padding-top: 24px; font-size: 15px; color: var(--muted); flex-shrink: 0; }
.aside-bottom summary { cursor: pointer; }
dl { margin: 14px 0; font-size: 15px; overflow-wrap: anywhere; }
dt { color: var(--muted); margin-top: 10px; }
dd { margin: 0; white-space: pre-wrap; }
main { padding: 40px 44px; min-width: 0; }
header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 20px; }
.scope { font-size: 16px; color: var(--muted); margin: 0 0 6px; overflow-wrap: anywhere; }
h1 { font-size: 36px; line-height: 1.25; font-weight: 600; letter-spacing: -.7px; margin: 0; }
h2 { font-size: 25px; line-height: 1.4; font-weight: 600; margin: 0; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; }
.primary { border-color: #7b8086; font-weight: 600; }
.project-stats { display: flex; flex-wrap: wrap; gap: 24px; margin: 28px 0 20px; color: var(--muted); font-size: 16px; }
.project-stats strong { font-size: 24px; color: #202124; margin-right: 5px; font-weight: 600; }
.activity-panel { border: 1px solid var(--line); border-radius: 8px; padding: 16px 20px; margin: 12px 0; }
.activity-panel > summary { cursor: pointer; font-weight: 600; }
.agent-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(210px,1fr)); gap: 20px; padding-top: 20px; }
.agent-card { min-width: 0; padding: 0 0 10px; }
.agent-card > a { color: #244d7e; font-weight: 600; text-decoration: none; overflow-wrap: anywhere; }
.agent-card p { margin: 8px 0; font-size: 15px; color: var(--muted); }
.agent-card .topic-link { color: inherit; text-decoration: none; font-size: 16px; overflow-wrap: anywhere; }
.agent-card .topic-link:hover { text-decoration: underline; }
.session-details { font-size: 14px; color: var(--muted); margin-top: 10px; }
.session-details summary { cursor: pointer; }
.work-row { border-top: 1px solid var(--line); padding: 16px 0 4px; margin-top: 15px; }
.work-row p { margin: 5px 0; overflow-wrap: anywhere; }
.work-row a { color: #244d7e; }
.toolbar { display: flex; flex-wrap: wrap; gap: 20px; align-items: center; margin: 30px 0 22px; }
.toolbar input[type=search] { max-width: 400px; }
.toolbar label { white-space: nowrap; }
#notice { color: #315c43; margin-top: 18px; font-size: 16px; }
#notice:empty, footer:empty { display: none; }
#notice.error, #compose-error { color: #a52222; }
.split { display: grid; grid-template-columns: minmax(240px, 34%) minmax(0, 1fr); border-top: 1px solid var(--line); min-height: 490px; }
.thread-list { border-right: 1px solid var(--line); min-width: 0; }
.thread { display: block; text-align: left; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; width: 100%; padding: 22px 20px; background: #fff; }
.thread.active { background: var(--selected); }
.thread strong { display: block; font-size: 19px; font-weight: 600; line-height: 1.5; overflow-wrap: anywhere; }
.thread p { margin: 7px 0 0; overflow-wrap: anywhere; }
.muted, small { color: var(--muted); font-size: 15px; }
.detail { padding: 28px 32px; min-width: 0; }
.detail h2 { overflow-wrap: anywhere; }
.message { margin-top: 28px; border-top: 1px solid var(--line); padding-top: 24px; }
.conversation { border: 1px solid var(--line); border-radius: 8px; margin: 14px 0; background: #fff; overflow: hidden; }
.conversation[open] { border-color: #b7c9de; }
.conversation > summary { padding: 22px 24px; cursor: pointer; display: block; position: relative; padding-right: 48px; }
.conversation > summary::after { content: '+'; position: absolute; right: 22px; top: 24px; color: var(--muted); font-size: 22px; }
.conversation[open] > summary::after { content: '−'; }
.conversation summary::-webkit-details-marker, .message > summary::-webkit-details-marker { display: none; }
.conversation h2 { font-size: 23px; letter-spacing: -.3px; margin: 4px 0 9px; overflow-wrap: anywhere; }
.conversation .conversation-meta { display: flex; flex-wrap: wrap; gap: 8px 18px; color: var(--muted); font-size: 15px; }
.conversation .preview { color: #60676e; margin: 12px 0 0; font-size: 17px; overflow-wrap: anywhere; }
.conversation[open] .preview { display: none; }
.conversation-body { border-top: 1px solid var(--line); padding: 22px 26px 28px; }
.topic-tag { font-size: 12px; text-transform: uppercase; letter-spacing: .8px; font-weight: 650; color: #38597d; }
.topic-tag.blocker { color: #a52222; }
.thread-tools { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 22px; }
.thread-tools button { font-size: 14px; padding: 7px 11px; }
.message { margin: 14px 0 0; padding: 0; border: 1px solid var(--line); border-radius: 7px; }
.message > summary { list-style: none; cursor: pointer; display: flex; align-items: center; gap: 13px; padding: 16px 18px; background: #fafbfc; }
.message[open] > summary { border-bottom: 1px solid var(--line); }
.avatar { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 8px; background: #eaf0f7; color: #35597f; font-size: 13px; font-weight: 700; flex-shrink: 0; }
.sender { flex: 1; min-width: 0; }
.sender strong { font-size: 16px; overflow-wrap: anywhere; }
.message-preview { display: block; color: var(--muted); font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 64ch; }
.message[open] .message-preview { display: none; }
.message time { font-size: 14px; color: var(--muted); text-align: right; }
.message-body { padding: 18px 22px 22px; }
.message-body > .meta { margin: 0 0 16px; font-size: 14px; }
.message-headers { margin-top: 18px; border-top: 1px solid var(--line); padding-top: 12px; font-size: 14px; color: var(--muted); }
.message-headers summary { cursor: pointer; }
.message-headers dl { display: grid; grid-template-columns: minmax(80px,125px) minmax(0,1fr); gap: 8px 16px; }
.message-headers dt { margin: 0; }
.message-headers dd { color: #41474e; }
.message-headers button { padding: 0; border: 0; color: #244d7e; text-align: left; font-size: inherit; overflow-wrap: anywhere; }
.markdown { font-size: 18px; line-height: 1.75; overflow-wrap: anywhere; }
.markdown > :first-child { margin-top: 0; }
.markdown > :last-child { margin-bottom: 0; }
.markdown h3, .markdown h4 { margin: 24px 0 10px; font-size: 21px; line-height: 1.4; }
.markdown h4 { font-size: 18px; }
.markdown p { white-space: pre-wrap; margin: 12px 0; }
.markdown a { color: #245c96; text-underline-offset: 3px; }
.markdown code { font: .87em ui-monospace, SFMono-Regular, monospace; background: #f2f4f6; border-radius: 4px; padding: 2px 5px; }
.markdown pre { font: 14px/1.7 ui-monospace, SFMono-Regular, monospace; background: #f6f8fa; border: 1px solid var(--line); padding: 16px; border-radius: 6px; white-space: pre-wrap; overflow-wrap: anywhere; }
.markdown ul, .markdown ol { padding-left: 25px; }
.markdown blockquote { border-left: 3px solid #c8d5e3; padding-left: 16px; margin-left: 0; color: var(--muted); }
.load-more { margin: 20px auto; display: block; }
.announcement pre { font-size: 19px; line-height: 1.75; font-family: inherit; white-space: pre-wrap; overflow-wrap: anywhere; color: #202124; max-width: 72ch; }
.meta { font-size: 16px; color: var(--muted); line-height: 1.7; overflow-wrap: anywhere; }
.pill { display: inline-block; background: var(--selected); color: #50555a; padding: 3px 8px; border-radius: 4px; font-size: 14px; margin: 8px 8px 0 0; }
.pill.finished { background: transparent; padding-left: 0; }
.empty { padding: 72px 20px; text-align: center; color: var(--muted); line-height: 1.8; }
.cards { display: grid; }
.announcement { border-top: 1px solid var(--line); padding: 28px 0; }
.announcement h2 { margin: 10px 0; }
.table-wrap { overflow: auto; border-top: 1px solid var(--line); }
table { border-collapse: collapse; width: 100%; min-width: 660px; text-align: left; }
th { font-size: 16px; color: var(--muted); font-weight: 500; padding: 18px 14px; vertical-align: top; }
td { padding: 20px 14px; border-top: 1px solid var(--line); font-size: 17px; max-width: 300px; overflow-wrap: anywhere; vertical-align: top; }
footer { margin-top: 28px; font-size: 15px; color: var(--muted); line-height: 1.6; }
dialog { border: 1px solid #c9cdd1; border-radius: 7px; background: #fff; color: inherit; width: min(720px, calc(100vw - 32px)); max-height: calc(100dvh - 32px); padding: 32px; overflow-y: auto; }
dialog::backdrop { background: #0005; }
.dialog-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 18px; }
.dialog-head button { font-size: 26px; line-height: 1; padding: 8px 12px; }
.dialog-actions { text-align: right; margin-top: 20px; }
[hidden] { display: none !important; }
@media (max-width: 1100px) {
  body { grid-template-columns: 250px minmax(0, 1fr); }
  aside { padding: 32px 20px; }
  main { padding: 32px 26px; }
  .split { grid-template-columns: 1fr; }
  .thread-list { max-height: 320px; overflow: auto; border-right: 0; border-bottom: 1px solid var(--line); }
  .detail { padding: 26px 20px; }
}
@media (max-width: 640px) {
  body { display: block; }
  aside { position: static; width: auto; height: auto; border-right: 0; border-bottom: 1px solid var(--line); padding: 24px 20px 16px; }
  .brand { margin-bottom: 22px; }
  .aside-bottom { padding-top: 14px; }
  .view-switch { grid-template-columns: 1fr 1fr; margin-bottom: 18px; }
  .inbox-list { max-height: 260px; }
  nav { display: flex; flex-wrap: wrap; margin-top: 16px; gap: 2px; }
  nav button { font-size: 16px; padding: 10px 8px; }
  main { padding: 26px 20px; }
  h1 { font-size: 30px; }
  h2 { font-size: 24px; }
  .actions button { font-size: 16px; }
  .toolbar { gap: 14px; }
  .detail { padding: 24px 4px; }
  .thread { padding: 20px 12px; }
  .announcement pre { font-size: 18px; }
  .conversation > summary { padding: 18px 36px 18px 16px; }
  .conversation h2 { font-size: 20px; }
  .conversation-body { padding: 16px 12px; }
  .message > summary { padding: 12px 10px; gap: 8px; flex-wrap: wrap; }
  .message-body { padding: 16px 12px; }
  .message-headers dl { grid-template-columns: 1fr; gap: 4px; }
  .message-headers dd { margin-bottom: 7px; }
  dialog { padding: 24px 20px; }
}

'''

JS = r'''
'use strict';
const $ = s => document.querySelector(s), enc = encodeURIComponent;
const state = {view:'human',project:'',address:'',tab:'mail',projects:[],inboxes:[],rows:[],overview:null,
  directoryEpoch:0,scopeEpoch:0,listEpoch:0,openThreads:new Set(),messageOpen:new Map(),cards:new Map(),
  threadData:new Map(),loads:new Map(),selectedThread:'',nextCursor:null,total:0,lastAgents:new Map(),mode:'mail'};
const element = (tag,text,cls) => {const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
const button = (text,fn,cls) => {const b=element('button',text,cls);b.type='button';b.onclick=fn;return b;};
const human = () => state.view==='human';
const date = x => x?new Date(x).toLocaleString():'Not recorded';
function relative(x){const seconds=Math.round((Date.now()-new Date(x).getTime())/1000);if(!Number.isFinite(seconds))return 'Not recorded';if(seconds<0)return date(x);if(seconds<60)return 'just now';if(seconds<3600)return `${Math.floor(seconds/60)}m ago`;if(seconds<86400)return `${Math.floor(seconds/3600)}h ago`;return `${Math.floor(seconds/86400)}d ago`;}
function time(x){const e=element('time',x?relative(x):'Not recorded');if(x){e.dateTime=x;e.title=`${date(x)} · ${x}`;}return e;}
function duration(ms){if(ms<0||!Number.isFinite(ms))return 'Clock order differs';const s=Math.round(ms/1000);if(s<60)return `${s}s`;if(s<3600)return `${Math.floor(s/60)}m ${s%60}s`;return `${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m`;}
function notice(text,error=false){$('#notice').textContent=text;$('#notice').className=error?'error':'';}
const fail = e => notice(e.message,true);
async function api(path,body,key){const r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json',...(key?{'Idempotency-Key':key}:{})}:{},body:body?JSON.stringify(body):undefined,cache:'no-store'});const data=await r.json();if(!r.ok)throw Error(data.error?.message||`Request failed (${r.status})`);return data;}
function saved(key){try{return localStorage.getItem(key)||'';}catch{return '';}}
function save(key,value){try{localStorage.setItem(key,value);}catch{}}
function route(){try{const parts=location.hash.slice(1).split('/').map(decodeURIComponent);if(parts[0]==='project'&&parts[1])return {view:'human',project:parts[1],thread:parts[2]==='thread'?parts[3]:''};if(parts[0].includes('@'))return {view:'agent',address:parts[0],project:parts[0].split('@')[1],thread:parts[1]==='thread'?parts[2]:''};}catch{}return null;}
function scopeHash(view=state.view,project=state.project,address=state.address){return view==='human'?`#project/${enc(project)}`:`#${enc(address)}`;}
function threadHash(id){return `${scopeHash()}/thread/${enc(id)}`;}
function topicLink(text,id){const a=element('a',text,'topic-link');a.href=threadHash(id);return a;}
function setThreadLink(id){state.selectedThread=id;history.replaceState(null,'',id?threadHash(id):scopeHash());}
function clearScope(){state.scopeEpoch++;state.listEpoch++;state.rows=[];state.rowsQuery='';state.overview=null;state.overviewSignature=null;state.openThreads.clear();state.messageOpen.clear();state.cards.clear();state.threadData.clear();state.loads.clear();state.selectedThread='';state.nextCursor=null;$('#search').value='';$('#content').replaceChildren();}
function updateHeader(){
  $('#scope').textContent=human()?'Human view · Project overview':`Agent view · ${state.address||'Choose an inbox'}`;
  $('#title').textContent=human()?(state.project||'Projects'):({mail:'Messages',announcements:'Announcements',reservations:'Reservations'}[state.tab]);
  $('#project-overview').hidden=!human();$('#compose').hidden=human()||state.tab==='reservations';
  $('#compose').textContent=state.tab==='announcements'?'New announcement':'New message';
  $('#unread').parentElement.hidden=human()||state.tab==='reservations';$('#history-label').hidden=state.tab!=='reservations';
  $('#search').placeholder=human()&&state.tab==='mail'?'Search project conversations…':'Search this view…';
  document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view===state.view)));
  document.querySelectorAll('[data-tab]').forEach(b=>{const selected=b.dataset.tab===state.tab;b.classList.toggle('selected',selected);if(selected)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
  document.querySelectorAll('.project-link,.inbox-link').forEach(a=>{const selected=a.classList.contains('project-link')?human()&&a.dataset.project===state.project:!human()&&a.dataset.address===state.address;a.classList.toggle('selected',selected);if(selected)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
}
function renderDirectory(){
  const root=$('#inboxes');root.replaceChildren();
  for(const project of state.projects){const group=element('section',undefined,'inbox-group'),link=element('a',undefined,'project-link');link.href=`#project/${enc(project.slug)}`;link.dataset.project=project.slug;link.append(element('span',project.slug),element('small',String(project.inbox_count)));group.append(link);
    for(const inbox of state.inboxes.filter(i=>i.project===project.slug)){const a=element('a',inbox.address,'inbox-link');a.href=`#${enc(inbox.address)}`;a.dataset.address=inbox.address;group.append(a);}root.append(group);}
  if(!state.projects.length)root.append(element('p','Registered projects and inboxes will appear here.','muted'));
  updateHeader();
}
async function applyRoute(){
  let target=route();if(!target){try{target=JSON.parse(saved('agent-inbox.location'));}catch{}}
  if(!target||!state.projects.some(p=>p.slug===target.project))target={view:'human',project:state.projects[0]?.slug||''};
  let remembered;try{remembered=JSON.parse(saved('agent-inbox.location'));}catch{}
  const candidates=state.inboxes.filter(i=>i.project===target.project);
  const address=candidates.find(i=>i.address===(target.address||state.lastAgents.get(target.project)||(remembered?.project===target.project?remembered.address:'')||state.address))?.address||candidates[0]?.address||'';
  if(target.view==='agent'&&!address)target.view='human';
  const changed=state.view!==target.view||state.project!==target.project||state.address!==address;
  if(changed)clearScope();state.view=target.view;state.project=target.project;state.address=address;
  if(address)state.lastAgents.set(state.project,address);
  if(target.thread){if(state.tab!=='mail'){clearScope();state.tab='mail';}state.selectedThread=target.thread;state.openThreads.add(target.thread);}
  save('agent-inbox.location',JSON.stringify({view:state.view,project:state.project,address:state.address}));
  if(state.project)history.replaceState(null,'',state.selectedThread?threadHash(state.selectedThread):scopeHash());updateHeader();await refresh();
}
async function connection(){try{const h=await api('/healthz');$('#connection-label').textContent=`Local service · v${h.version}`;const dl=$('#connection-details');dl.replaceChildren();for(const [label,value] of [['Service address',location.origin],['Listening IP',h.listen_address],['Port',h.port],['Browser peer IP',h.client_ip],['Uptime',duration(h.uptime_seconds*1000)],['Server time',date(h.server_time)]]){dl.append(element('dt',label),element('dd',value??'Not recorded'));}}catch(e){$('#connection-label').textContent='Service unavailable';fail(e);}}
async function reloadWorkspace(){const epoch=++state.directoryEpoch;try{const [directory,projects]=await Promise.all([api('/v1/inboxes'),api('/v1/projects')]);if(epoch!==state.directoryEpoch)return;state.inboxes=directory.inboxes;state.projects=projects.projects;renderDirectory();await applyRoute();await connection();}catch(e){if(epoch===state.directoryEpoch)fail(e);}}
window.addEventListener('hashchange',()=>{if(!$('#composer').open)applyRoute();});
function path(){return human()?`/v1/projects/${enc(state.project)}/threads`:`/v1/inboxes/${enc(state.address)}/threads`;}
async function refresh({quiet=false,append=false}={}){
  if(!state.project){$('#content').replaceChildren(element('div','Register an inbox to get started.','empty'));return;}
  if(quiet&&($('#composer').open||document.hidden))return;
  const epoch=++state.listEpoch,scope=state.scopeEpoch,tab=state.tab,query=$('#search').value;
  if(!quiet&&!append)notice('');if(!state.rows.length&&!quiet)$('#content').replaceChildren(element('div','Loading…','empty'));
  try{
    let url;
    if(tab==='mail')url=human()?`${path()}?limit=50&q=${enc($('#search').value)}${append&&state.nextCursor?'&cursor='+enc(state.nextCursor):''}`:`${path()}?observe=true&limit=200&unread=${$('#unread').checked}`;
    else if(tab==='announcements')url=human()?`/v1/projects/${enc(state.project)}/announcements`:`/v1/announcements?observe=true&inbox=${enc(state.address)}&unread=${$('#unread').checked}`;
    else url=`/v1/projects/${enc(state.project)}/reservations?history=${$('#history').checked?'1':'0'}&limit=200`;
    const [data,overview]=await Promise.all([api(url),human()?api(`/v1/projects/${enc(state.project)}/overview`):Promise.resolve(null)]);
    if(epoch!==state.listEpoch||scope!==state.scopeEpoch)return;
    const rows=data.threads||data.announcements||data.entries;
    const previousRows=JSON.stringify(state.rows), previous=new Map(state.rows.map(r=>[r.thread_id,r]));
    for(const row of rows)if(row.thread_id&&JSON.stringify(previous.get(row.thread_id))!==JSON.stringify(row))state.threadData.delete(row.thread_id);
    if(append){const found=new Set(state.rows.map(r=>r.thread_id));state.rows.push(...rows.filter(r=>!found.has(r.thread_id)));}
    else if(human()&&tab==='mail'&&query===state.rowsQuery&&state.rows.length>50){const merged=new Map(state.rows.map(r=>[r.thread_id,r]));for(const r of rows)merged.set(r.thread_id,r);state.rows=[...merged.values()].sort((a,b)=>b.activity_id-a.activity_id||b.thread_id.localeCompare(a.thread_id));}
    else state.rows=rows;
    if(append||state.rows.length<=50||query!==state.rowsQuery){state.nextCursor=data.next_cursor||null;}state.rowsQuery=query;state.total=data.total??rows.length;
    if(human()&&state.selectedThread&&!rows.some(r=>r.thread_id===state.selectedThread))state.threadData.delete(state.selectedThread);
    const overviewSignature=JSON.stringify(overview?{...overview,observed_at:null}:null);
    if(overviewSignature!==state.overviewSignature){state.overviewSignature=overviewSignature;state.overview=overview;renderOverview();}
    if(!quiet||JSON.stringify(state.rows)!==previousRows)render();else if(human()&&state.selectedThread){const card=state.cards.get(state.selectedThread);if(card?.open)loadThread(state.selectedThread,card.lastElementChild);}$('#updated').textContent=`Updated ${new Date().toLocaleTimeString([], {hour:'numeric',minute:'2-digit'})}`;
  }catch(e){if(epoch===state.listEpoch&&scope===state.scopeEpoch){if(!quiet&&!state.rows.length)$('#content').replaceChildren(element('div','Could not load this view. Use Refresh to try again.','empty'));fail(e);}}
}
function renderOverview(){
  const root=$('#project-overview'),o=state.overview;if(!o){root.replaceChildren();return;}
  const activityOpen=root.querySelector('.activity-panel')?.open??true,workOpen=root.querySelector('.work-panel')?.open??false;
  const stats=element('div',undefined,'project-stats');for(const [value,label] of [[o.agents.length,'agents'],[o.thread_count,'threads'],[o.message_count,'messages'],[o.reservation_count,'reservations']]){const s=element('span');s.append(element('strong',String(value)),document.createTextNode(label));stats.append(s);}
  const panel=element('details',undefined,'activity-panel');panel.open=activityOpen;panel.append(element('summary','Agent activity'));const grid=element('div',undefined,'agent-grid');
  for(const agent of o.agents){const card=element('article',undefined,'agent-card'),name=element('a',agent.address);name.href=`#${enc(agent.address)}`;card.append(name);const seen=element('p','Last seen ');seen.append(time(agent.last_seen_at));card.append(seen);
    if(agent.latest_message){card.append(topicLink(agent.latest_message.subject,agent.latest_message.thread_id));const sent=element('p','Last message ');sent.append(time(agent.latest_message.sent_at));card.append(sent);}else card.append(element('p','No messages sent yet.'));
    card.append(element('p',`${agent.task_count} assigned / claimed · ${agent.reservation_count} ${agent.reservation_count===1?'reservation':'reservations'}`));
    if(agent.sessions.length){const sessions=element('details',undefined,'session-details');sessions.append(element('summary',`${agent.sessions.length} recorded ${agent.sessions.length===1?'session':'sessions'}`));const dl=element('dl');for(const s of agent.sessions){dl.append(element('dt',s.session_id),element('dd',`${date(s.last_seen_at)}${s.pid?' · PID '+s.pid:''}`));}sessions.append(dl);card.append(sessions);}grid.append(card);}
  panel.append(grid);root.replaceChildren(stats,panel);
  if(o.task_count){const work=element('details',undefined,'activity-panel work-panel');work.open=workOpen;work.append(element('summary',`Tracked work · ${o.task_count} unfinished`));for(const task of o.tasks){const row=element('article',undefined,'work-row');row.append(element('strong',task.title),element('p',`${task.state} · ${task.owner||task.target||'Unassigned'}${task.session?' · '+task.session:''}`,'meta'));if(task.handoff)row.append(element('p',`Next: ${task.handoff.next_action}`));else if(task.note)row.append(element('p',task.note));if(task.thread_id)row.append(topicLink('Open conversation',task.thread_id));row.append(element('small',task.id));work.append(row);}if(o.tasks_truncated)work.append(element('p','Showing the 20 most recently updated tasks. Use the AE task list for the full queue.','muted'));root.append(work);}
}
function render(){
  const q=$('#search').value.toLowerCase(),rows=human()&&state.tab==='mail'?state.rows:state.rows.filter(r=>JSON.stringify(r).toLowerCase().includes(q));
  const root=$('#content');
  if(state.tab==='mail'){if(human())renderConversations(rows);else renderInbox(rows);}
  else if(state.tab==='announcements'){const cards=element('div',undefined,'cards');for(const r of rows){const card=element('article',undefined,'announcement');card.append(element('span',r.project||'All local projects','pill'),element('h2',r.subject),element('p',`${r.sender} · ${date(r.created_at)}`,'meta'),markdown(r.body_markdown));if(!human()){if(!r.read_at)card.append(button('Acknowledge',async()=>{const address=state.address;try{await api(`/v1/announcements/${enc(r.id)}/read`,{inbox:address});if(address===state.address)await refresh();}catch(e){fail(e);}}));else card.append(element('small',`Acknowledged ${date(r.read_at)}`));}cards.append(card);}root.replaceChildren(cards);}
  else {const wrap=element('div',undefined,'table-wrap'),table=element('table'),head=element('thead'),tr=element('tr');for(const title of ['Path / resource','Holder / session','Reason','State','Expires / released'])tr.append(element('th',title));head.append(tr);table.append(head);const body=element('tbody');for(const r of rows){const row=element('tr');row.append(element('td',r.path),element('td',`${r.holder}\n${r.session||'No session'}`),element('td',r.reason||'—'));const status=element('td');status.append(element('span',r.active?'Active':'Finished',`pill ${r.active?'':'finished'}`));row.append(status,element('td',date(r.released_at||r.expires_at)));body.append(row);}table.append(body);wrap.append(table);root.replaceChildren(wrap);}
  if(!rows.length&&!(state.tab==='mail'&&state.selectedThread&&!q))root.replaceChildren(element('div',q?'No matches. Try another search.':state.tab==='mail'?'No conversations here yet.':state.tab==='announcements'?'No announcements here yet.':'No reservations in this project.','empty'));
  $('#footer').textContent=human()&&state.tab==='mail'?`${state.rows.length} of ${state.total} conversations · Observing does not mark agent mail read.`:state.tab==='reservations'?'Use the CLI to reserve, renew or release. Active reservations and up to 200 finished records.':human()?'Project announcements · Agent acknowledgments stay unchanged.':'Agent inbox · Read state changes only when you choose Mark read. Showing up to 200 records.';
}
function conversationCard(id){
  let card=state.cards.get(id);if(card)return card;
  card=element('details',undefined,'conversation');card.dataset.id=id;card.append(element('summary','Linked conversation'),element('div',undefined,'conversation-body'));
  card.addEventListener('toggle',()=>{if(!card.isConnected)return;if(card.open){state.openThreads.add(id);setThreadLink(id);loadThread(id,card.lastElementChild);}else{state.openThreads.delete(id);if(state.selectedThread===id)setThreadLink('');}});
  state.cards.set(id,card);return card;
}
function renderConversations(rows){
  const root=$('#content');let feed=root.querySelector('.conversation-feed');if(!feed){feed=element('div',undefined,'conversation-feed');root.replaceChildren(feed);}
  const ids=new Set(rows.map(r=>r.thread_id));for(const child of [...feed.children])if(!ids.has(child.dataset.id)&&child.dataset.id!==state.selectedThread)child.remove();
  for(const row of rows){const card=conversationCard(row.thread_id);
    const signature=JSON.stringify(row);if(card.dataset.signature!==signature){const summary=card.firstElementChild,tag=row.subject.match(/^(Design|Release|Claim|Blocker):/i)?.[1]||'Conversation';summary.replaceChildren(element('span',tag,`topic-tag ${tag.toLowerCase()==='blocker'?'blocker':''}`),element('h2',row.subject));const meta=element('div',undefined,'conversation-meta');meta.append(element('span',`${row.message_count} ${row.message_count===1?'message':'messages'}`),element('span',row.participants.join(', ')),time(row.last_email_at));summary.append(meta,element('p',row.preview?.replace(/\s+/g,' ').slice(0,160)||'','preview'));if(card.dataset.signature)state.threadData.delete(row.thread_id);card.dataset.signature=signature;}
    if(card.parentElement!==feed)feed.append(card);else if(feed.lastElementChild!==card)feed.append(card);
    if(state.openThreads.has(row.thread_id))card.open=true;if(card.open)loadThread(row.thread_id,card.lastElementChild);
  }
  let more=root.querySelector('.load-more');if(state.nextCursor){if(!more){more=button('Load more conversations',()=>refresh({append:true}),'load-more');root.append(more);}}else more?.remove();
  if(state.selectedThread&&!ids.has(state.selectedThread)){const card=conversationCard(state.selectedThread);card.open=true;feed.prepend(card);loadThread(state.selectedThread,card.lastElementChild);}
}
function renderInbox(rows){const root=$('#content'),split=element('div',undefined,'split'),list=element('div',undefined,'thread-list'),detail=element('div',undefined,'detail');detail.id='detail';for(const r of rows){const b=button('',()=>{state.selectedThread=r.thread_id;setThreadLink(r.thread_id);loadThread(r.thread_id,detail);document.querySelectorAll('.thread').forEach(x=>x.classList.toggle('active',x.dataset.id===r.thread_id));});b.className='thread';b.dataset.id=r.thread_id;b.classList.toggle('active',state.selectedThread===r.thread_id);b.append(element('strong',r.subject),element('p',r.participants.join(', '),'muted'),time(r.last_email_at));if(r.unread_count)b.append(element('span',`${r.unread_count} unread`,'pill'));for(const role of r.your_roles||[])b.append(element('span',role==='to'?'To · owner':'CC · observer','pill'));list.append(b);}split.append(list,detail);root.replaceChildren(split);if(state.selectedThread)loadThread(state.selectedThread,detail);else detail.append(element('div','Select a conversation to see this agent’s view.','empty'));}
async function loadThread(id,target){
  const cached=state.threadData.get(id);if(cached){if(target.dataset.rendered!==JSON.stringify(cached))renderThread(cached,target);return;}
  if(state.loads.has(id))return;const epoch=state.scopeEpoch,mode=state.view,address=state.address;
  const token={};state.loads.set(id,token);target.replaceChildren(element('p','Loading conversation…','muted'));
  try{const data=await api(`${path()}/${enc(id)}${human()?'':'?observe=true'}`);if(epoch!==state.scopeEpoch||mode!==state.view||address!==state.address)return;state.threadData.set(id,data);if(target.isConnected)renderThread(data,target);else if(!human()&&state.selectedThread===id&&$('#detail'))renderThread(data,$('#detail'));}
  catch(e){if(epoch===state.scopeEpoch&&target.isConnected){target.replaceChildren(element('p',e.message,'error'),button('Try again',()=>loadThread(id,target)));}}
  finally{if(state.loads.get(id)===token)state.loads.delete(id);}
}
function inline(parent,text){
  const regex=/(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\)|https?:\/\/[^\s<>]+)/g;let start=0;
  for(const match of text.matchAll(regex)){parent.append(document.createTextNode(text.slice(start,match.index)));const value=match[0];if(value.startsWith('`'))parent.append(element('code',value.slice(1,-1)));else if(value.startsWith('**'))parent.append(element('strong',value.slice(2,-2)));else{const link=value.startsWith('[')?value.match(/^\[([^\]]+)\]\((.*)\)$/):null;const a=element('a',link?link[1]:value);a.href=link?link[2]:value;a.target='_blank';a.rel='noopener noreferrer';parent.append(a);}start=match.index+value.length;}parent.append(document.createTextNode(text.slice(start)));
}
function markdown(text){
  const root=element('div',undefined,'markdown'),lines=String(text||'').split('\n');let paragraph=[],list=null;
  const flush=()=>{if(paragraph.length){const p=element('p');inline(p,paragraph.join('\n'));root.append(p);paragraph=[];}list=null;};
  for(let i=0;i<lines.length;i++){const line=lines[i];if(/^\s*```/.test(line)){flush();const code=[];while(++i<lines.length&&!/^\s*```/.test(lines[i]))code.push(lines[i]);root.append(element('pre',code.join('\n')));}
    else if(/^#{1,6}\s/.test(line)){flush();const match=line.match(/^(#+)\s+(.*)$/),h=element(match[1].length<3?'h3':'h4');inline(h,match[2]);root.append(h);}
    else if(/^\s*(?:[-*]|\d+\.)\s+/.test(line)){if(paragraph.length)flush();const ordered=/^\s*\d+\./.test(line),tag=ordered?'OL':'UL';if(!list||list.tagName!==tag){list=element(tag.toLowerCase());root.append(list);}const item=element('li');inline(item,line.replace(/^\s*(?:[-*]|\d+\.)\s+/,''));list.append(item);}
    else if(/^>\s?/.test(line)){flush();const quote=element('blockquote');inline(quote,line.replace(/^>\s?/,''));root.append(quote);}
    else if(!line.trim())flush();else{list=null;paragraph.push(line);}}
  flush();return root;
}
function renderThread(t,target){
  target.dataset.rendered=JSON.stringify(t);target.replaceChildren();if(!human())target.append(element('h2',t.subject),element('p',`Reading as ${t.reading_as}`,'meta'));
  else {const card=target.parentElement;if(card.firstElementChild.textContent==='Linked conversation')card.firstElementChild.textContent=t.subject;}
  const first=t.emails[0],last=t.emails.at(-1);if(first)target.append(element('p',`${t.emails.length} ${t.emails.length===1?'message':'messages'} · ${date(first.sent_at)}${last!==first?' → '+date(last.sent_at):''}`,'meta'));
  const tools=element('div',undefined,'thread-tools');for(const [label,open] of [['Expand messages',true],['Collapse messages',false]])tools.append(button(label,()=>{target.querySelectorAll('.message').forEach(m=>{state.messageOpen.set(m.dataset.id,open);m.open=open;});}));
  tools.append(button('Copy thread link',async()=>{try{await navigator.clipboard.writeText(location.origin+location.pathname+threadHash(t.thread_id));notice('Thread link copied.');}catch{notice('Copy the thread URL from your address bar.');}}));
  if(!human()){const address=state.address;tools.append(button('Mark read',async()=>{try{await api(`/v1/inboxes/${enc(address)}/threads/${enc(t.thread_id)}/read`,{});if(address===state.address&&!human()){state.threadData.delete(t.thread_id);await refresh();notice('Marked read.');}}catch(e){fail(e);}}));}target.append(tools);
  t.emails.forEach((m,index)=>{const card=element('details',undefined,'message');card.dataset.id=m.email_id;card.id=m.email_id;card.open=state.messageOpen.has(m.email_id)?state.messageOpen.get(m.email_id):index===t.emails.length-1;card.addEventListener('toggle',()=>state.messageOpen.set(m.email_id,card.open));const summary=element('summary'),sender=element('span',undefined,'sender');sender.append(element('strong',m.from),element('span',m.body_markdown.replace(/\s+/g,' ').slice(0,140),'message-preview'));summary.append(element('span',m.from.split('@')[0].slice(0,2).toUpperCase(),'avatar'),sender,time(m.sent_at));card.append(summary);
    const body=element('div',undefined,'message-body');const routing=`To: ${m.to.join(', ')}${m.cc.length?' · CC: '+m.cc.join(', '):''}`;body.append(element('p',routing,'meta'),markdown(m.body_markdown));
    const headers=element('details',undefined,'message-headers');headers.open=state.messageOpen.get('headers:'+m.email_id)||false;headers.addEventListener('toggle',()=>state.messageOpen.set('headers:'+m.email_id,headers.open));headers.append(element('summary','Message details'));const dl=element('dl');const fields=[['From',m.from],['To',m.to.join(', ')],['CC',m.cc.join(', ')||'None'],['Sent',`${date(m.sent_at)}\n${m.sent_at}`],['Agent session',m.sender_session||'Not recorded'],['API peer IP',m.api_peer_ip||'Not recorded'],['Received by API',date(m.api_received_at)],['Message ID',m.email_id]];
    if(index)fields.push(['Since previous message',duration(new Date(m.sent_at)-new Date(t.emails[index-1].sent_at))]);if(!human())fields.push(['Inbox role',m.your_role]);for(const [label,value] of fields)dl.append(element('dt',label),element('dd',value));
    if(m.reply_to_email_id){const dd=element('dd');dd.append(button(m.reply_to_email_id,()=>{const parent=[...target.querySelectorAll('.message')].find(x=>x.dataset.id===m.reply_to_email_id);if(parent){parent.open=true;parent.scrollIntoView({block:'nearest',behavior:'smooth'});}}));dl.append(element('dt','Reply to'),dd);}
    dl.append(element('dt','Recipient receipts'),element('dd',(m.receipts||[]).map(r=>`${r.address} · ${r.kind.toUpperCase()} · ${r.read_at?'read '+date(r.read_at):'unread'}`).join('\n')));headers.append(dl,element('p','API peer is the client address seen by this service, not an agent’s physical location. Historical and synced mail may have no recorded peer.','muted'));body.append(headers);card.append(body);target.append(card);
  });if(last&&!human())target.append(button('Reply to thread',()=>compose(last),'primary'));
}
function compose(reply=null){if(human()||!state.address){notice('Choose View as agent and an inbox to compose.');return;}state.mode=reply?'reply':state.tab==='announcements'?'announcement':'mail';state.reply=reply;state.composeAddress=state.address;state.key=crypto.randomUUID();$('#compose-form').reset();$('#compose-error').textContent='';$('#compose-title').textContent=reply?'Reply to thread':state.mode==='announcement'?'New announcement':'New message';$('#sending-as').textContent=`Sending as ${state.composeAddress}`;$('#recipients').hidden=state.mode==='announcement';$('#subject-label').hidden=!!reply;$('#subject').required=!reply;$('#to').required=state.mode!=='announcement';$('#global-label').hidden=state.mode!=='announcement';$('#send').textContent=state.mode==='announcement'?'Publish announcement':'Send message';if(reply){$('#to').value=[...new Set([reply.from,...reply.to])].filter(a=>a!==state.composeAddress).join(', ');$('#cc').value=reply.cc.filter(a=>a!==state.composeAddress&&!$('#to').value.split(', ').includes(a)).join(', ');}$('#composer').showModal();}
$('#compose-form').addEventListener('input',()=>{state.key=crypto.randomUUID();});
$('#compose-form').onsubmit=async e=>{e.preventDefault();const sender=state.composeAddress,key=state.key,split=id=>$(id).value.split(',').map(s=>s.trim()).filter(Boolean);let url='/v1/emails',body={from:sender,subject:$('#subject').value,body_markdown:$('#body').value};if(state.mode==='announcement'){url='/v1/announcements';body.all_projects=$('#global').checked;}else{body.to=split('#to');body.cc=split('#cc');if(state.mode==='reply')url=`/v1/emails/${enc(state.reply.email_id)}/reply`;}$('#compose-form').querySelectorAll('input,textarea,button').forEach(e=>e.disabled=true);$('#compose-error').textContent='';try{const result=await api(url,body,key);$('#composer').close();state.threadData.clear();await refresh();notice(state.mode==='announcement'?'Announcement published locally.':`Message accepted: ${result.delivery_status||'local service'}.`);}catch(error){$('#compose-error').textContent=error.message;}finally{$('#compose-form').querySelectorAll('input,textarea,button').forEach(e=>e.disabled=false);}};
$('#composer').addEventListener('cancel',e=>{if($('#send').disabled)e.preventDefault();});$('#cancel').onclick=()=>$('#composer').close();$('#compose').onclick=()=>compose();$('#refresh').onclick=()=>{state.threadData.clear();reloadWorkspace();};$('#unread').onchange=()=>refresh();$('#history').onchange=()=>refresh();
let searchTimer;$('#search').oninput=()=>{clearTimeout(searchTimer);if(human()&&state.tab==='mail'){setThreadLink('');searchTimer=setTimeout(()=>refresh(),200);}else render();};
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{if(b.dataset.view==='agent'&&!state.address){notice('This project has no registered agent inboxes.');return;}const row=state.rows.find(r=>r.thread_id===state.selectedThread),thread=state.threadData.get(state.selectedThread),participants=row?.participants||thread?.emails.flatMap(m=>[m.from,...m.to,...m.cc])||[];const keep=state.selectedThread&&(b.dataset.view==='human'||participants.includes(state.address));location.hash=scopeHash(b.dataset.view)+(keep?'/thread/'+enc(state.selectedThread):'');});
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{if(state.tab===b.dataset.tab)return;clearScope();state.tab=b.dataset.tab;history.replaceState(null,'',scopeHash());updateHeader();refresh();});
setInterval(()=>{document.querySelectorAll('time[datetime]').forEach(t=>t.textContent=relative(t.dateTime));if($('#live').checked)refresh({quiet:true});},15000);
reloadWorkspace();
'''

ASSETS = {
    '/': ('text/html; charset=utf-8', HTML),
    '/ui': ('text/html; charset=utf-8', HTML),
    '/ui/': ('text/html; charset=utf-8', HTML),
    '/ui.css': ('text/css; charset=utf-8', CSS),
    '/ui.js': ('text/javascript; charset=utf-8', JS),
}
