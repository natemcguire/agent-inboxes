JS = r'''
async function initializeHosted(){
  if(state.member)return;
  state.member=await api('/v1/hosted/me');
  state.registration=await api('/v1/hosted/registration/setup',{}).catch(error=>{state.registrationError=error.message;return null;});
  const account=element('section',undefined,'hosted-account');
  account.append(element('p',state.member.email,'muted'),button('Connect an agent',openConnections),element('p','Agent self-registration is on','muted'));
  const logout=element('a','Sign out','muted');logout.href='/cdn-cgi/access/logout';account.append(logout);
  $('#connection').before(account);
}
async function openConnections(){
  if(!state.registration)state.registration=await api('/v1/hosted/registration/setup',{}).catch(error=>{state.registrationError=error.message;return null;});
  let dialog=$('#connections');if(dialog)dialog.remove();
  dialog=element('dialog');dialog.id='connections';
  dialog.innerHTML='<div class="dialog-head"><h2>Connect an agent</h2><button type="button" aria-label="Close connections">×</button></div><p>Give each agent a name and a project. Its key connects directly to this shared workspace.</p><p class="muted">First time? <a href="https://github.com/natemcguire/agent-inboxes# get-started" target="_blank" rel="noopener">Install the CLI</a> on your computer.</p><form id="key-form"><label>Project<input name="project" placeholder="boats" required maxlength="64" pattern="[a-z0-9][a-z0-9._-]*"></label><label>Agent name<input name="family" placeholder="codex-nate" required maxlength="48" pattern="[a-z0-9][a-z0-9._-]*"></label><label>Computer or description<input name="label" placeholder="Nate’s Mac · Codex" maxlength="100"></label><p class="muted">Keys expire after 90 days. Revoke a key below when an agent no longer needs access.</p><button class="primary" type="submit">Create agent key</button><p id="key-error" role="alert"></p></form><section id="key-result" hidden><h3>Your connection commands</h3><p>Copy these into the agent’s terminal, from its project. The key is shown once.</p><textarea id="key-commands" rows="8" readonly aria-label="Agent connection commands"></textarea><button id="copy-connection">Copy commands</button></section><section id="registration-setup"><h3>Agent self-registration is on</h3><p>Your sign-in automatically enabled registration. Your agents can register themselves in any project under your account. Save the setup credential once on each trusted machine. Access lasts until you revoke it; revoking it also revokes the agent keys it created.</p><p>On each trusted machine, run this setup once. Then agents register without another human approval.</p><textarea id="registration-command" rows="9" readonly aria-label="One-time registration setup"></textarea><button id="copy-registration">Copy setup commands</button><div id="registration-list"></div></section><h3>Your agents</h3><div id="key-list"></div>';
  dialog.querySelector('a').href='https://github.com/natemcguire/agent-inboxes#get-started';
  document.body.append(dialog);
  dialog.querySelector('[aria-label="Close connections"]').onclick=()=>dialog.close();
  dialog.addEventListener('close',()=>dialog.remove(),{once:true});
  const form=dialog.querySelector('form');form.elements.project.value=state.project;
  form.elements.family.value=`codex-${state.member.human_agent}`;
  async function list(){
    const data=await api('/v1/hosted/tokens'),root=dialog.querySelector('#key-list');root.replaceChildren();
    for(const key of data.tokens){
      const row=element('article',undefined,'work-row');row.append(element('strong',`${key.family}@${key.project}`),element('p',`${key.label} · ${key.revoked_at?'Revoked':`Expires ${date(key.expires_at*1000)}`}`,'muted'));
      if(!key.revoked_at)row.append(button('Revoke key',async()=>{try{await api(`/v1/hosted/tokens/${key.id}/revoke`,{});await list();}catch(e){dialog.querySelector('#key-error').textContent=e.message;}}));root.append(row);
    }
    if(!data.tokens.length)root.append(element('p','No agents connected yet.','muted'));
  }
  async function listRegistrars(){
    const data=await api('/v1/hosted/registrars'),root=dialog.querySelector('#registration-list');root.replaceChildren();
    for(const key of data.registrars){
      const row=element('article',undefined,'work-row');row.append(element('strong',key.label),element('p',key.revoked_at?'Revoked':'Active until revoked','muted'));
      if(!key.revoked_at)row.append(button('Revoke registration access',async()=>{try{await api(`/v1/hosted/registrars/${key.id}/revoke`,{});if(key.id===state.registration?.id){dialog.querySelector('#registration-command').value='Registration access revoked. Reload the inbox to set up fresh access.';dialog.querySelector('#copy-registration').disabled=true;}await listRegistrars();await list();}catch(e){dialog.querySelector('#key-error').textContent=e.message;}}));root.append(row);
    }
  }
  const registration=state.registration;
  dialog.querySelector('#copy-registration').disabled=!registration;
  dialog.querySelector('#registration-command').value=registration?`agent-inbox hosted login --url '${location.origin}' --credential-stdin <<'INBOX_CREDENTIAL'\n${registration.token}\nINBOX_CREDENTIAL\n\n# Each agent can now run, using its project and name:\neval "$(agent-inbox hosted register --url '${location.origin}' --project my-project --agent codex)"\neval "$(agent-inbox claim)"\nagent-inbox brief`:`Setup is temporarily unavailable: ${state.registrationError}. Reopen this dialog to retry.`;
  dialog.querySelector('#copy-registration').onclick=async()=>{try{await navigator.clipboard.writeText(dialog.querySelector('#registration-command').value);dialog.querySelector('#copy-registration').textContent='Copied';}catch{dialog.querySelector('#registration-command').select();}};
  form.onsubmit=async event=>{
    event.preventDefault();const submit=form.querySelector('button[type=submit]');submit.disabled=true;dialog.querySelector('#key-error').textContent='';
    try{
      const data=await api('/v1/hosted/tokens',{project:form.elements.project.value,family:form.elements.family.value,label:form.elements.label.value||form.elements.family.value});
      dialog.querySelector('#key-commands').value=`export AGENT_INBOX_URL='${location.origin}'\nexport AGENT_INBOX_TOKEN='${data.token}'\nexport AGENT_INBOX_PROJECT='${data.project}'\nexport AGENT_INBOX_AGENT='${data.family}'\neval "$(agent-inbox claim)"\nagent-inbox brief`;
      dialog.querySelector('#key-result').hidden=false;
      await list();await reloadWorkspace();
    }catch(error){dialog.querySelector('#key-error').textContent=error.message;}finally{submit.disabled=false;}
  };
  dialog.querySelector('#copy-connection').onclick=async()=>{try{await navigator.clipboard.writeText(dialog.querySelector('#key-commands').value);dialog.querySelector('#copy-connection').textContent='Copied';}catch{dialog.querySelector('#key-commands').select();}};
  dialog.showModal();try{await list();await listRegistrars();}catch(e){dialog.querySelector('#key-error').textContent=e.message;}
}
'''
