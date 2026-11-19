// Isolated test authority: signed identities plus mutable product grants.
// Production uses the actual auth hub, never this fixture.
let revoked = new Set();
const loggedOut = new Set();
const decode = value => Uint8Array.from(atob(value.replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0));
export default {async fetch(request, env) {
  const path = new URL(request.url).pathname;
  const json = (data,status=200) => Response.json(data,{status});
  if(request.method === 'GET') return json({ok:true});
  const body = await request.json();
  if(path === '/test/revoke'){revoked.add(body.email);return json({ok:true});}
  if(path === '/test/restore'){revoked.delete(body.email);return json({ok:true});}
  const allowed = email => ['nate@example.com','josh@example.com','maker@example.com','second@example.com'].includes(email) && !revoked.has(email);
  if(path === '/internal/entitlement') return json({active:allowed(body.email),tenant_id:'nates-software'});
  try {
    const [head,payload,signature] = body.token.split('.');
    const meta = JSON.parse(new TextDecoder().decode(decode(head)));
    const c = JSON.parse(new TextDecoder().decode(decode(payload)));
    const key = await crypto.subtle.importKey('jwk',JSON.parse(env.JWKS).keys[0],{name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['verify']);
    const valid = await crypto.subtle.verify('RSASSA-PKCS1-v1_5',key,decode(signature),new TextEncoder().encode(head+'.'+payload));
    if(!valid || meta.alg !== 'RS256' || c.iss !== 'https://test-auth.example' || !c.aud.includes(body.audience) || c.exp <= Date.now()/1000 || !allowed(c.email)) throw new Error();
    if(path === '/session/logout'){loggedOut.add(body.token);return json({ok:true});}
    if(loggedOut.has(body.token))return json({active:false},401);
    const user={active:true,sub:c.email,email:c.email,name:c.email,tenant_id:'nates-software',role:'member',products:['agent-inboxes'],exp:c.exp};
    return json(path === '/session/exchange' ? {token:body.token,user,tenant_id:'nates-software'} : user);
  } catch {return json({active:false},401);}
}};
