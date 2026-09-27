"""Write deployment configuration for a workspace using Nate's shared SSO.

Register the product and allowed callback origin in the auth hub first. This
script creates no accounts, membership grants, or Cloudflare Access policies.
"""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--account', required=True)
parser.add_argument('--domain', required=True)
parser.add_argument('--workspace', default='team')
parser.add_argument('--name', default='Agent Inbox')
parser.add_argument('--tenant', default='nates-software')
parser.add_argument('--app', default='agent-inboxes')
parser.add_argument('--owner', required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parent
config = json.loads((root/'wrangler.jsonc').read_text())
config.update(account_id=args.account, routes=[{'pattern':args.domain,'custom_domain':True}])
config['vars'].update(WORKSPACE=args.workspace, WORKSPACE_NAME=args.name,
    SSO_TENANT_ID=args.tenant, SSO_APP_ID=args.app, MEMBERS=args.owner,
    WORKSPACE_HOSTS=json.dumps({args.domain:args.workspace}))
output = root/'wrangler.production.local.json'
output.write_text(json.dumps(config,indent=2)+'\n')
output.chmod(0o600)
print('Deploy with: cd cloudflare && npm run deploy -- --config wrangler.production.local.json')
