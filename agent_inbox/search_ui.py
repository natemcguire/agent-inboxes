"""Conversation search palette: live results, filters, and keyboard navigation."""
HTML = '''<section class="finder" aria-label="Conversation search">
<div class="finder-input"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></svg><input id="conversation-search" type="search" role="combobox" aria-label="Search conversations" aria-autocomplete="list" aria-expanded="false" aria-controls="search-results" placeholder="Find a conversation, a decision, a detail…" autocomplete="off" spellcheck="false" maxlength="500"><kbd id="search-shortcut">⌘ K</kbd></div>
<div id="search-panel" class="finder-panel" hidden>
<div class="finder-filters"><button id="search-all" class="filter-chip" aria-pressed="true">All projects</button><button id="search-current" class="filter-chip" hidden></button><button id="search-person" class="filter-chip">From someone <span>↓</span></button><button id="search-recent" class="filter-chip">Past week</button></div>
<div class="finder-status"><span id="search-status" role="status" aria-live="polite">Recent conversations</span><span id="search-count"></span></div><div id="search-correction" hidden></div><div id="search-history" hidden></div>
<div id="search-results" role="listbox" aria-label="Conversation results"></div><button id="search-more" hidden>Show more conversations</button>
<div class="finder-footer"><span><kbd>↑</kbd><kbd>↓</kbd> navigate <kbd>↵</kbd> open <kbd>esc</kbd> close</span><span>Use “quotes” for an exact phrase</span></div>
</div></section>'''

CSS = '''
.finder { position: relative; z-index: 20; margin: -4px 0 34px; }
.finder-input { display: flex; align-items: center; gap: 15px; min-height: 60px; padding: 4px 17px; border: 1px solid #d8dde3; border-radius: 12px; background: #fbfcfd; transition: box-shadow .15s,border-color .15s; }
.finder-input:focus-within { border-color: #779ac3; background: white; box-shadow: 0 0 0 4px #edf3fa; }
.finder-input svg { fill: none; stroke: #737e8b; stroke-width: 1.7; stroke-linecap: round; width: 23px; height: 23px; flex-shrink: 0; }
.finder-input input { width: 100%; min-width: 0; padding: 12px 0; border: 0; background: transparent; font-size: 19px; box-shadow: none; outline: none; }
.finder-input input:focus { outline: none; box-shadow: none; }
.finder-input input::placeholder { color: #77818b; }
.finder kbd { font-family: inherit; font-size: 12px; line-height: 1.6; color: #697582; border: 1px solid #dde2e7; border-radius: 4px; padding: 1px 5px; white-space: nowrap; background: #fff; }
.finder-panel { position: absolute; top: calc(100% + 10px); width: 100%; min-width: 0; border: 1px solid #dbe1e7; border-radius: 14px; background: white; box-shadow: 0 20px 55px #263c5826,0 3px 10px #263c5810; overflow: hidden; }
.finder-filters { display: flex; gap: 7px; padding: 15px 17px 10px; flex-wrap: wrap; }
.filter-chip { border: 1px solid #e1e5e9; border-radius: 20px; padding: 6px 11px; font-size: 13px; color: #56616f; background: white; }
.filter-chip[aria-pressed=true] { border-color: #d5e2f2; color: #285582; background: #eef4fc; }
.filter-chip span { color: #8a96a2; padding-left: 5px; }
.finder-status { display: flex; justify-content: space-between; gap: 12px; padding: 9px 19px 12px; color: #77818e; font-size: 12px; letter-spacing: .3px; }
#search-status { color: #596574; font-weight: 600; }
#search-results { max-height: min(55vh,570px); overflow-y: auto; overscroll-behavior: contain; padding: 0 7px 7px; }
.search-result { display: block; position: relative; width: 100%; border: 1px solid transparent; border-radius: 8px; padding: 14px 15px; text-align: left; background: white; }
#search-results[aria-busy=true] { opacity: .55; pointer-events: none; }
.search-result + .search-result { margin-top: 3px; }
.search-result[aria-selected=true] { background: #f0f5fb; border-color: #e1ebf7; }
.search-result:hover { background: #f5f8fc; }
.search-result-title { display: block; font-size: 18px; font-weight: 600; color: #202b38; line-height: 1.45; padding-right: 24px; overflow-wrap: anywhere; }
.search-result[aria-selected=true]::after { content: '↵'; position: absolute; right: 16px; top: 17px; color: #7d92ac; font-size: 17px; }
.search-result-excerpt { color: #5b6774; font-size: 15px; line-height: 1.6; margin: 6px 0 10px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.search-result mark { color: #213f5e; background: #ffeec0; border-radius: 2px; padding: 0 1px; }
.search-result-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; color: #7b8592; font-size: 12px; }
.search-project { color: #42668b; background: #edf2f8; border-radius: 4px; padding: 2px 6px; font-weight: 550; }
.search-result-meta time { margin-left: auto; }
.finder-footer { border-top: 1px solid #edf0f3; display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; padding: 11px 18px; font-size: 11px; color: #87919c; background: #fcfdfe; }
.finder-footer kbd { font-size: 10px; margin: 0 3px; }
#search-correction { padding: 2px 19px 12px; font-size: 14px; color: #667483; }
#search-correction button { border: 0; padding: 0; color: #285582; font-size: inherit; text-decoration: underline; text-underline-offset: 3px; }
#search-history { padding: 0 18px 10px; display: flex; gap: 6px; flex-wrap: wrap; }
#search-history button { font-size: 12px; padding: 4px 9px; border: 1px solid #edf0f3; border-radius: 5px; color: #6f7d8c; max-width: 240px; text-overflow: ellipsis; overflow: hidden; white-space: nowrap; }
.finder-empty { padding: 38px 20px 45px; text-align: center; }
.finder-empty strong { display: block; color: #455364; font-size: 18px; margin-bottom: 8px; }
.finder-empty p { color: #84909c; font-size: 14px; margin: 0; }
#search-more { display: block; width: 100%; border: 0; border-top: 1px solid #edf0f3; border-radius: 0; color: #355f8a; font-size: 14px; padding: 13px; }
.message.search-target { border-color: #9dbbdc; box-shadow: 0 0 0 3px #eef4fb; scroll-margin-top: 30px; }
.message.search-target > summary { background: #edf4fc; }
@media(max-width:640px) { .finder { margin: 0 0 25px; } .finder-input { min-height: 54px; gap: 10px; padding: 3px 12px; } .finder-input input { font-size: 16px; } #search-shortcut { display: none; } .finder-panel { left: -8px; width: calc(100% + 16px); } .finder-filters { padding: 12px 11px 5px; gap: 6px; } .finder-footer > span:last-child { display: none; } .search-result { padding: 12px; } .search-result-title { font-size: 16px; } .search-result-excerpt { font-size: 14px; } }
@media(prefers-reduced-motion:reduce) { .finder-input { transition: none; } }
'''

JS = r'''
(() => {
  const input=$('#conversation-search'),panel=$('#search-panel'),list=$('#search-results'),finder=input.closest('.finder');
  let sequence=0,controller,timer,items=[],active=-1,scope='',next=null,shown=[];
  function historyItems(){try{return JSON.parse(saved('agent-inbox.searches')||'[]').filter(x=>typeof x==='string').slice(0,5);}catch{return [];}}
  function remember(){const q=input.value.trim();if(q)save('agent-inbox.searches',JSON.stringify([q,...historyItems().filter(x=>x!==q)].slice(0,5)));}
  function open(){panel.hidden=false;input.setAttribute('aria-expanded','true');updateScopes();}
  function close(){panel.hidden=true;input.setAttribute('aria-expanded','false');input.removeAttribute('aria-activedescendant');controller?.abort();clearTimeout(timer);sequence++;}
  function updateScopes(){const current=$('#search-current');current.hidden=!state.project;current.textContent=!human()?'This inbox':state.project;current.setAttribute('aria-pressed',String(scope==='current'));$('#search-all').setAttribute('aria-pressed',String(!scope));}
  function choose(index){active=index;list.querySelectorAll('[role=option]').forEach((row,i)=>row.setAttribute('aria-selected',String(i===index)));if(index>=0&&items[index]){input.setAttribute('aria-activedescendant','search-option-'+index);list.children[index]?.scrollIntoView({block:'nearest'});}else input.removeAttribute('aria-activedescendant');}
  function marked(target,spans){for(const span of spans)target.append(span.match?element('mark',span.text):document.createTextNode(span.text));}
  function draw(entries){items=entries;list.removeAttribute('aria-busy');list.replaceChildren();entries.forEach((entry,index)=>{const row=button('',entry.action,'search-result');row.id='search-option-'+index;row.setAttribute('role','option');row.setAttribute('aria-selected','false');row.tabIndex=-1;const title=element('span',undefined,'search-result-title');marked(title,entry.title);row.append(title);if(entry.excerpt){const excerpt=element('p',undefined,'search-result-excerpt');marked(excerpt,entry.excerpt);row.append(excerpt);}const meta=element('div',undefined,'search-result-meta');if(entry.project)meta.append(element('span',entry.project,'search-project'));if(entry.meta)meta.append(element('span',entry.meta));if(entry.date)meta.append(time(entry.date));row.append(meta);row.onmouseenter=()=>choose(index);row.onmousedown=e=>e.preventDefault();list.append(row);});choose(entries.length?0:-1);}
  function resultEntry(result){return {title:result.title,excerpt:result.excerpt,project:result.project,date:result.sent_at,meta:`${result.sender} · ${input.value.trim()?result.match_count+' matching '+(result.match_count===1?'message':'messages'):result.message_count+' messages'}`,action:()=>{remember();close();const base=scope==='current'&&!human()?scopeHash('agent',state.project,state.address):scopeHash('human',result.project);location.hash=`${base}/thread/${enc(result.thread_id)}/message/${enc(result.email_id)}`;if(route()?.message===state.focusedMessage)applyRoute();}};}
  function filterPicker(){const match=input.value.match(/(?:^|\s)(project|from):([^\s"]*)$/i);if(!match)return false;const kind=match[1].toLowerCase(),query=match[2].toLowerCase();const choices=kind==='project'?state.projects.map(p=>({value:p.slug,meta:p.thread_count+' conversations'})):state.inboxes.map(i=>({value:i.address,meta:i.display_name||i.project}));const entries=choices.filter(x=>x.value.toLowerCase().includes(query)).slice(0,20).map(x=>({title:[{text:x.value,match:false}],meta:x.meta,action:()=>{input.value=input.value.slice(0,input.value.length-match[0].length)+(match[0].startsWith(' ')?' ':'')+kind+':'+x.value+' ';input.focus();search();}}));$('#search-status').textContent=kind==='project'?'Choose a project':'Choose a person or agent';$('#search-count').textContent='';$('#search-correction').hidden=true;$('#search-history').hidden=true;$('#search-more').hidden=true;draw(entries);if(entries.length)choose(0);else empty('No matching '+(kind==='project'?'projects':'people'),'Keep typing or press Escape to close.');return true;}
  function empty(title,description){const box=element('div',undefined,'finder-empty');box.append(element('strong',title),element('p',description));list.replaceChildren(box);}
  async function search(append=false){open();controller?.abort();controller=new AbortController();const version=++sequence;clearTimeout(timer);items=[];active=-1;input.removeAttribute('aria-activedescendant');if(filterPicker())return;
    const query=input.value.trim(),params=new URLSearchParams({q:query,limit:'12'});if(scope==='current'&&state.project){params.set('project',state.project);if(!human())params.set('inbox',state.address);}if(append&&next!==null)params.set('offset',String(next));else shown=[];
    $('#search-status').textContent='Searching…';$('#search-count').textContent='';list.setAttribute('aria-busy','true');$('#search-more').hidden=true;
    try {const response=await fetch('/v1/search?'+params,{signal:controller.signal,cache:'no-store'});const data=await response.json();if(!response.ok)throw Error(data.error?.message||'Search unavailable');if(version!==sequence)return;shown.push(...data.results);next=data.next_offset;
      $('#search-status').textContent=query?'Conversations':'Recent conversations';$('#search-count').textContent=data.total?`${data.total} ${data.total===1?'conversation':'conversations'}`:'';
      const correction=$('#search-correction');correction.replaceChildren();correction.hidden=!data.correction;if(data.correction){correction.append(document.createTextNode('Closest matches for '),button(data.correction,()=>{input.value=data.correction;search();}));}
      const recent=$('#search-history');recent.replaceChildren();recent.hidden=!!query;for(const q of query?[]:historyItems())recent.append(button(q,()=>{input.value=q;search();}));
      draw(shown.map(resultEntry));if(!shown.length)empty(query?'No conversations found':'Your conversations will appear here',query?'Try fewer words, a different spelling, or remove a filter.':'Search the full history as your agents start talking.');$('#search-more').hidden=next===null;
    }catch(error){if(error.name==='AbortError'||version!==sequence)return;draw([]);$('#search-status').textContent='Search unavailable';empty('Couldn’t search right now',error.message);}
    finally{if(version===sequence)list.removeAttribute('aria-busy');}
  }
  function addFilter(prefix){input.value=input.value.trimEnd()+(input.value.trim()?' ':'')+prefix;input.focus();search();}
  input.addEventListener('focus',()=>search());
  input.addEventListener('input',()=>{controller?.abort();sequence++;clearTimeout(timer);open();items=[];choose(-1);list.setAttribute('aria-busy','true');list.querySelectorAll('button').forEach(b=>b.disabled=true);$('#search-count').textContent='';$('#search-more').hidden=true;$('#search-status').textContent='Searching…';timer=setTimeout(()=>search(),120);});
  input.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();close();input.blur();}else if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();if(panel.hidden){search();return;}if(items.length)choose(active<0?(e.key==='ArrowDown'?0:items.length-1):(active+(e.key==='ArrowDown'?1:-1)+items.length)%items.length);}else if(e.key==='Enter'){e.preventDefault();if(panel.hidden){search();return;}if(items.length)items[active<0?0:active].action();}else if(e.key==='Tab'&&active>=0&&/(?:^|\s)(project|from):[^\s"]*$/i.test(input.value)){e.preventDefault();items[active]?.action();}});
  document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'&&!$('#composer').open){e.preventDefault();input.focus();input.select();if(panel.hidden)search();}});
  document.addEventListener('pointerdown',e=>{if(!finder.contains(e.target))close();});
  document.addEventListener('focusin',e=>{if(!finder.contains(e.target))close();});
  document.addEventListener('inbox:scope',()=>{updateScopes();if(!panel.hidden)search();});
  $('#search-all').onclick=()=>{scope='';search();};$('#search-current').onclick=()=>{scope='current';search();};
  $('#search-person').onclick=()=>addFilter('from:');
  $('#search-recent').onclick=()=>{const d=new Date();d.setDate(d.getDate()-7);input.value=input.value.replace(/\bafter:\S+\s*/g,'').trim();addFilter('after:'+d.toISOString().slice(0,10)+' ');};
  $('#search-more').onclick=()=>search(true);
  $('#search-shortcut').textContent=/Mac|iPhone|iPad/.test(navigator.platform)?'⌘ K':'Ctrl K';
})();
'''
