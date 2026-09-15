"""Bundled, dependency-free browser client. All resources are served locally."""

HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>Agent Inbox</title><link rel="stylesheet" href="/ui.css"><script src="/ui.js" defer></script></head>
<body><aside><a class="brand" href="/">Agent Inbox</a>
<h2 class="sidebar-title" id="inboxes-title">Inboxes</h2><div id="inboxes" class="inbox-list" role="navigation" aria-labelledby="inboxes-title"><p class="muted">Loading inboxes…</p></div>
<nav aria-label="Workspace"><button data-tab="mail" class="selected" aria-current="page">Messages</button><button data-tab="announcements">Announcements</button><button data-tab="reservations">Reservations</button></nav>
<div class="aside-bottom" id="connection">Connecting…</div></aside>
<main><header><div><p class="scope" id="scope">Choose an inbox</p><h1 id="title">Messages</h1></div><div class="actions"><button id="refresh">Refresh</button><button id="compose" class="primary">New message</button></div></header>
<div id="notice" role="status" aria-live="polite"></div><div class="toolbar"><input id="search" type="search" placeholder="Search this view…" aria-label="Search this view"><label><input id="unread" type="checkbox"> Unread only</label><label id="history-label" hidden><input id="history" type="checkbox" checked> Include history</label></div>
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
button:focus-visible, input:focus-visible, textarea:focus-visible, a:focus-visible { outline: 2px solid #2457a6; outline-offset: 3px; }
input:not([type=checkbox]), textarea { width: 100%; min-width: 0; background: #fff; border: 1px solid #c9cdd1; border-radius: 5px; padding: 11px 13px; }
input::placeholder, textarea::placeholder { color: #6b7075; opacity: 1; }
textarea { resize: vertical; line-height: 1.65; }
label { display: block; color: var(--muted); font-size: 16px; }
label input:not([type=checkbox]), label textarea { margin: 8px 0 20px; font-size: 18px; }
input[type=checkbox] { width: 17px; height: 17px; margin: 0 7px 0 0; vertical-align: -2px; accent-color: #40454b; }
aside { padding: 40px 24px; background: #fff; border-right: 1px solid var(--line); display: flex; flex-direction: column; position: sticky; top: 0; height: 100vh; min-width: 0; }
.brand { font-size: 25px; color: #202124; text-decoration: none; font-weight: 650; margin-bottom: 30px; flex-shrink: 0; }
.sidebar-title { font-size: 17px; font-weight: 600; margin: 0 0 10px; flex-shrink: 0; }
.inbox-list { min-height: 0; overflow-y: auto; margin: 0 -8px; padding: 0 8px; }
.inbox-group + .inbox-group { margin-top: 18px; }
.inbox-group h3 { font-size: 15px; font-weight: 500; color: var(--muted); margin: 0 0 4px; overflow-wrap: anywhere; }
.inbox-link { display: block; color: inherit; text-decoration: none; font-size: 17px; padding: 10px 12px; border-radius: 5px; overflow-wrap: anywhere; }
.inbox-link:hover { background: #f5f6f7; }
.inbox-link.selected { background: var(--selected); font-weight: 600; }
nav { display: grid; gap: 6px; margin-top: 24px; padding-top: 18px; border-top: 1px solid var(--line); flex-shrink: 0; }
nav button { text-align: left; border-color: transparent; padding: 12px; }
nav button.selected { background: var(--selected); font-weight: 600; }
.aside-bottom { margin-top: auto; padding-top: 24px; font-size: 15px; color: var(--muted); flex-shrink: 0; }
main { padding: 40px 44px; min-width: 0; }
header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 20px; }
.scope { font-size: 16px; color: var(--muted); margin: 0 0 6px; overflow-wrap: anywhere; }
h1 { font-size: 36px; line-height: 1.25; font-weight: 600; letter-spacing: -.7px; margin: 0; }
h2 { font-size: 25px; line-height: 1.4; font-weight: 600; margin: 0; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; }
.primary { border-color: #7b8086; font-weight: 600; }
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
.message pre, .announcement pre { font-size: 19px; line-height: 1.75; font-family: inherit; white-space: pre-wrap; overflow-wrap: anywhere; color: #202124; max-width: 72ch; }
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
  .aside-bottom { display: none; }
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
  .message pre, .announcement pre { font-size: 18px; }
  dialog { padding: 24px 20px; }
}

'''

JS = r'''
'use strict';
const $=s=>document.querySelector(s), enc=encodeURIComponent;
const state={address:'',inboxes:[],directoryEpoch:0,tab:'mail',rows:[],thread:null,epoch:0,readEpoch:0,mode:'mail',reply:null,key:null};
const element=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
const button=(text,fn,cls)=>{const b=element('button',text,cls);b.type='button';b.onclick=fn;return b;};
const date=x=>x?new Date(x).toLocaleString():'—';
function notice(text,error=false){$('#notice').textContent=text;$('#notice').className=error?'error':'';}
async function api(path,body,key){const r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json',...(key?{'Idempotency-Key':key}:{})}:{},body:body?JSON.stringify(body):undefined,cache:'no-store'});const data=await r.json();if(!r.ok)throw Error(data.error?.message||`Request failed (${r.status})`);return data;}
function fail(e){notice(e.message,true);}
function path(){return `/v1/inboxes/${enc(state.address)}/threads`;}
function reset(){state.epoch++;state.readEpoch++;state.thread=null;state.rows=[];$('#content').replaceChildren();}
function linkedAddress(){try{return decodeURIComponent(location.hash.slice(1));}catch{return '';}}
function selectInbox(address){if(address!==state.address){reset();state.address=address;$('#search').value='';}$('#scope').textContent=address||'Choose an inbox';document.querySelectorAll('.inbox-link').forEach(a=>{const selected=a.dataset.address===address;a.classList.toggle('selected',selected);if(selected)a.setAttribute('aria-current','true');else a.removeAttribute('aria-current');});if(address&&linkedAddress()!==address)history.replaceState(null,'',`#${enc(address)}`);}
function renderInboxes(){const root=$('#inboxes');root.replaceChildren();let project=null,group;for(const inbox of state.inboxes){const name=inbox.address.split('@')[1];if(name!==project){project=name;group=element('section',undefined,'inbox-group');group.append(element('h3',project));root.append(group);}const link=element('a',inbox.address,'inbox-link');link.href=`#${enc(inbox.address)}`;link.dataset.address=inbox.address;group.append(link);}if(!state.inboxes.length)root.append(element('p','Registered inboxes will appear here.','muted'));selectInbox(state.address);}
async function reloadWorkspace(){const epoch=++state.directoryEpoch;try{const {inboxes}=await api('/v1/inboxes');if(epoch!==state.directoryEpoch)return;state.inboxes=inboxes.sort((a,b)=>a.project.localeCompare(b.project)||a.address.localeCompare(b.address));const requested=linkedAddress()||state.address;renderInboxes();selectInbox(inboxes.some(i=>i.address===requested)?requested:inboxes[0]?.address||'');await refresh();}catch(e){if(epoch===state.directoryEpoch){if(!state.inboxes.length)$('#inboxes').replaceChildren(element('p','Could not load inboxes. Use Refresh to try again.','muted'));fail(e);}}}
window.addEventListener('hashchange',()=>{const address=linkedAddress();if(state.inboxes.some(i=>i.address===address)){selectInbox(address);refresh();}});
async function refresh(){const epoch=++state.epoch;state.readEpoch++;state.thread=null;state.rows=[];$('#content').replaceChildren(element('div','Loading…','empty'));notice('');try{
 if(!state.address){$('#content').replaceChildren(element('div','Choose an inbox from the sidebar to get started.','empty'));return;}
 const address=state.address;let data;
 if(state.tab==='mail')data=(await api(`${path()}?limit=200&unread=${$('#unread').checked}`)).threads;
 else if(state.tab==='announcements')data=(await api(`/v1/announcements?inbox=${enc(address)}&unread=${$('#unread').checked}`)).announcements;
 else data=(await api(`/v1/projects/${enc(address.split('@')[1])}/reservations?history=${$('#history').checked?'1':'0'}&limit=200`)).entries;
 if(epoch!==state.epoch)return;state.rows=data;render();
 }catch(e){if(epoch===state.epoch){$('#content').replaceChildren(element('div','Could not load this view. Use Refresh to try again.','empty'));fail(e);}}}
function render(){const q=$('#search').value.toLowerCase();const rows=state.rows.filter(r=>JSON.stringify(r).toLowerCase().includes(q));const root=$('#content');root.replaceChildren();
 if(!rows.length){root.append(element('div',q?'No matches. Try another search.':state.tab==='mail'?'No messages here yet. Start a conversation with another agent.':state.tab==='announcements'?'No announcements in this inbox.':'No reservations in this project.','empty'));return;}
 if(state.tab==='mail'){const split=element('div',undefined,'split'),list=element('div',undefined,'thread-list'),detail=element('div',undefined,'detail');detail.id='detail';detail.append(element('div','Select a conversation to read its messages.','empty'));for(const r of rows){const b=button('',()=>readThread(r.thread_id));b.className='thread';b.dataset.id=r.thread_id;b.append(element('strong',r.subject),element('p',r.participants.join(', '),'muted'),element('p',date(r.last_email_at),'muted'));if(r.unread_count)b.append(element('span',`${r.unread_count} unread`,'pill'));for(const role of r.your_roles||[])b.append(element('span',role==='to'?'To · owner':'CC · observer','pill'));list.append(b);}split.append(list,detail);root.append(split);if(state.thread)renderThread();}
 else if(state.tab==='announcements'){const cards=element('div',undefined,'cards');for(const r of rows){const card=element('article',undefined,'announcement');card.append(element('span',r.project||'All local projects','pill'),element('h2',r.subject),element('p',`${r.sender} · ${date(r.created_at)}`,'meta'),element('pre',r.body_markdown));if(!r.read_at)card.append(button('Acknowledge',async()=>{const address=state.address;try{await api(`/v1/announcements/${enc(r.id)}/read`,{inbox:address});if(address===state.address)await refresh();}catch(e){fail(e);}}));else card.append(element('small',`Acknowledged ${date(r.read_at)}`));cards.append(card);}root.append(cards);}
 else{const wrap=element('div',undefined,'table-wrap'),table=element('table'),head=element('thead'),tr=element('tr');for(const title of ['Path / resource','Holder / session','Reason','State','Expires / released'])tr.append(element('th',title));head.append(tr);table.append(head);const body=element('tbody');for(const r of rows){const row=element('tr');row.append(element('td',r.path),element('td',`${r.holder}\n${r.session||'No session'}`),element('td',r.reason||'—'));const status=element('td');status.append(element('span',r.active?'Active':'Finished',`pill ${r.active?'':'finished'}`));row.append(status,element('td',date(r.released_at||r.expires_at)));body.append(row);}table.append(body);wrap.append(table);root.append(wrap);}}
async function readThread(id){const epoch=state.epoch,reading=++state.readEpoch,address=state.address;try{const result=await api(`${path()}/${enc(id)}`);if(epoch!==state.epoch||reading!==state.readEpoch||address!==state.address)return;state.thread=result;renderThread();}catch(e){if(epoch===state.epoch&&reading===state.readEpoch)fail(e);}}
function renderThread(){const t=state.thread,detail=$('#detail');if(!t||!detail)return;detail.replaceChildren(element('h2',t.subject),element('p',`Reading as ${t.reading_as}`,'meta'));document.querySelectorAll('.thread').forEach(b=>b.classList.toggle('active',b.dataset.id===t.thread_id));const address=state.address;detail.append(button('Mark read',async()=>{try{await api(`/v1/inboxes/${enc(address)}/threads/${enc(t.thread_id)}/read`,{});if(address===state.address){notice('Marked read.');await refresh();}}catch(e){fail(e);}}));for(const m of t.emails){const card=element('article',undefined,'message');card.append(element('span',`Your role: ${m.your_role}`,'pill'),element('p',`From ${m.from} · ${date(m.sent_at)}`,'meta'),element('p',`To: ${m.to.join(', ')}${m.cc.length?' · CC: '+m.cc.join(', '):''}`,'meta'),element('pre',m.body_markdown));detail.append(card);}const latest=t.emails.at(-1);if(latest)detail.append(button('Reply to thread',()=>compose(latest),'primary'));
}
function compose(reply=null){if(!state.address){notice('Choose an inbox before composing.',true);$('.inbox-link')?.focus();return;}state.mode=reply?'reply':state.tab==='announcements'?'announcement':'mail';state.reply=reply;state.key=crypto.randomUUID();$('#compose-form').reset();$('#compose-error').textContent='';$('#compose-title').textContent=reply?'Reply to thread':state.mode==='announcement'?'New announcement':'New message';$('#sending-as').textContent=`Sending as ${state.address}`;$('#recipients').hidden=state.mode==='announcement';$('#subject-label').hidden=!!reply;$('#subject').required=!reply;$('#to').required=state.mode!=='announcement';$('#global-label').hidden=state.mode!=='announcement';$('#send').textContent=state.mode==='announcement'?'Publish announcement':'Send message';if(reply){$('#to').value=[...new Set([reply.from,...reply.to])].filter(a=>a!==state.address).join(', ');$('#cc').value=reply.cc.filter(a=>a!==state.address&&!$('#to').value.split(', ').includes(a)).join(', ');}$('#composer').showModal();}
$('#compose-form').addEventListener('input',()=>{state.key=crypto.randomUUID();});
$('#compose-form').onsubmit=async e=>{e.preventDefault();const sender=state.address,key=state.key;const split=id=>$(id).value.split(',').map(s=>s.trim()).filter(Boolean);let url='/v1/emails',body={from:sender,subject:$('#subject').value,body_markdown:$('#body').value};if(state.mode==='announcement'){url='/v1/announcements';body.all_projects=$('#global').checked;}else{body.to=split('#to');body.cc=split('#cc');if(state.mode==='reply')url=`/v1/emails/${enc(state.reply.email_id)}/reply`;}$('#compose-form').querySelectorAll('input,textarea,button').forEach(e=>e.disabled=true);$('#compose-error').textContent='';try{const result=await api(url,body,key);$('#composer').close();await refresh();notice(state.mode==='announcement'?'Announcement published locally.':`Message accepted: ${result.delivery_status||'local service'}.`);}catch(error){$('#compose-error').textContent=error.message;}finally{$('#compose-form').querySelectorAll('input,textarea,button').forEach(e=>e.disabled=false);}};
$('#composer').addEventListener('cancel',e=>{if($('#send').disabled)e.preventDefault();});$('#cancel').onclick=()=>$('#composer').close();$('#compose').onclick=()=>compose();$('#refresh').onclick=reloadWorkspace;$('#unread').onchange=refresh;$('#history').onchange=refresh;$('#search').oninput=render;
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{reset();state.tab=b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(x=>{x.classList.toggle('selected',x===b);if(x===b)x.setAttribute('aria-current','page');else x.removeAttribute('aria-current');});$('#title').textContent=b.textContent.replace(/^[^A-Za-z]+/,'');$('#compose').hidden=state.tab==='reservations';$('#compose').textContent=state.tab==='announcements'?'New announcement':'New message';$('#unread').parentElement.hidden=state.tab==='reservations';$('#history-label').hidden=state.tab!=='reservations';$('#footer').textContent=state.tab==='reservations'?'Use the CLI to reserve, renew or release. Showing active reservations and up to 200 finished records.':'Showing up to 200 records.';$('#search').value='';refresh();});
(async()=>{try{const health=await api('/healthz');$('#connection').textContent=`Local service · v${health.version}`;}catch(e){$('#connection').textContent='Service unavailable';fail(e);}await reloadWorkspace();})();
'''

ASSETS = {
    '/': ('text/html; charset=utf-8', HTML),
    '/ui': ('text/html; charset=utf-8', HTML),
    '/ui/': ('text/html; charset=utf-8', HTML),
    '/ui.css': ('text/css; charset=utf-8', CSS),
    '/ui.js': ('text/javascript; charset=utf-8', JS),
}
