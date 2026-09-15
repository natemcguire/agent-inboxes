"""Create invite-only Access login and write a private deployment configuration.

The API path intentionally delegates authentication to the Worker so scoped
agent keys work without browser login. The Worker verifies Access JWTs for
humans and hashed, revocable project keys for agents on every request.
"""
import argparse
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--account', required=True)
parser.add_argument('--domain', required=True)
parser.add_argument('--team', required=True, help='Cloudflare Access team subdomain')
parser.add_argument('--members', required=True, help='Comma-separated login email addresses')
parser.add_argument('--workspace', default='team')
parser.add_argument('--name', default='Agent Inbox')
args = parser.parse_args()
root = Path(__file__).resolve().parent
headers = {'Content-Type':'application/json'}
if os.environ.get('CLOUDFLARE_API_KEY') and os.environ.get('CLOUDFLARE_EMAIL'):
    headers.update({'X-Auth-Key':os.environ['CLOUDFLARE_API_KEY'],'X-Auth-Email':os.environ['CLOUDFLARE_EMAIL']})
else:
    headers['Authorization']='Bearer '+os.environ['CLOUDFLARE_API_TOKEN']


def call(path, body=None, method=None):
    request=urllib.request.Request('https://api.cloudflare.com/client/v4/accounts/'+args.account+'/access/'+path,
        headers=headers, data=json.dumps(body).encode() if body is not None else None,method=method)
    try:
        with urllib.request.urlopen(request,timeout=30) as response: data=json.load(response)
    except urllib.error.HTTPError as exc:
        data=json.loads(exc.read())
    if not data.get('success'):
        raise RuntimeError(json.dumps(data.get('errors')))
    return data['result']


try: organization=call('organizations')
except RuntimeError as exc:
    if 'not_enabled' not in str(exc):raise
    organization=call('organizations',{'name':args.name,'auth_domain':args.team+'.cloudflareaccess.com','session_duration':'24h'})
providers=call('identity_providers')
otp=next((p for p in providers if p['type']=='onetimepin'),None)
if not otp:otp=call('identity_providers',{'name':'Email code','type':'onetimepin','config':{}})
apps=call('apps')
members=[email.strip().lower() for email in args.members.split(',') if email.strip()]
if not members or any('@' not in email for email in members):raise ValueError('Specify valid member emails')


def configure(domain, payload):
    existing=next((app for app in apps if app.get('domain')==domain),None)
    if existing:
        if existing.get('name') != payload['name']:
            raise RuntimeError('An unrelated Access application already owns '+domain)
        return call('apps/'+existing['id'], payload, 'PUT')
    return call('apps',payload)


login=configure(args.domain,{'name':args.name,'type':'self_hosted','domain':args.domain,'session_duration':'24h',
    'allowed_idps':[otp['id']],'auto_redirect_to_identity':True,
    'policies':[{'name':'Workspace members','decision':'allow','include':[{'email':{'email':email}} for email in members]}]})
configure(args.domain+'/v1',{'name':args.name+' API — authenticated by Worker','type':'self_hosted','domain':args.domain+'/v1','app_launcher_visible':False,
    'policies':[{'name':'Worker validates signed login or scoped agent key','decision':'bypass','include':[{'everyone':{}}]}]})
config=json.loads((root/'wrangler.jsonc').read_text())
config.update(account_id=args.account,routes=[{'pattern':args.domain,'custom_domain':True}])
config['vars'].update(WORKSPACE=args.workspace,WORKSPACE_NAME=args.name,ACCESS_DOMAIN=organization['auth_domain'],ACCESS_AUD=login['aud'],MEMBERS=','.join(members))
output=root/'wrangler.production.local.json'
output.write_text(json.dumps(config,indent=2)+'\n')
output.chmod(0o600)
print('Configured invite-only login for',len(members),'members.')
print('Deploy with: cd cloudflare && npm run deploy -- --config wrangler.production.local.json')
