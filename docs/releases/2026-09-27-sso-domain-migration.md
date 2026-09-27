# Shared SSO and domain migration, 2026-09-27

The organization is `nates-software`, owned by `nate.mcguire@gmail.com`.
Manage invitations and individual product grants at
https://auth.nates-software.com/admin. Account approval alone does not grant
access. No other user received automatic organization membership.

## Domains

| Product | Canonical URL | East Bay behavior |
| --- | --- | --- |
| Agent Inbox | https://inbox.nates-software.com | `inboxes.eastbayprojects.com` is a separate workspace in the same Worker. |
| Tasks | https://tasks.nates-software.com | `tasks.eastbayprojects.com` uses the same Worker, database, files and workspaces. |
| Suite | https://nates-software.com | Uses the shared SSO hub. |
| View | https://view.nates-software.com | Uses the shared SSO hub. |
| Share | https://share.nates-software.com | Product reads and creation require an invited member with Share access. |

The old singular `inbox.eastbayprojects.com` redirects browser traffic with
HTTP 308. Its `/v1/` API remains temporarily compatible for existing clients;
new registrations use the canonical domain. The two obsolete Cloudflare Access
applications were removed after the replacement was deployed.

## Inbox data and authorization

The `agent-inboxes` Worker and historical Durable Object storage key
`eastbayprojects` remain unchanged. This preserves messages, project keys,
registrars, task coordination and file reservations. The separate East Bay
workspace uses `eastbayprojects-branded`; canonical keys cannot access it.

Only credential ownership belonging to `nate@eastbayprojects.com` was migrated
to `nate.mcguire@gmail.com`. Other historical owners/records remain intact but
cannot authenticate without a current SSO membership and Inbox product grant.
No email aliases are accepted as authentication proof.

Browser callbacks validate state, exchange a single-use hub token and set a
Secure, HttpOnly, host-only cookie. Each request introspects its current tenant
and product entitlement. Machine keys also check their human owner's live
entitlement through the private `AUTH_HUB` service binding, then recheck local
key validity inside the transaction. The former Access headers are ignored.

## This Mac

Updated shared `~/Projects/PREFERENCES.md`, `AI-GET-STARTED.md`, `AGENTS.md`,
`CLAUDE.md`, the ai-startup guide/config/launcher sources, private environment
URLs, and the Muse LaunchAgent plist. Existing process environments are not
rewritten; reload a project's hosted registration to get canonical exports:

```sh
eval "$(agent-inbox hosted register --project YOUR_PROJECT --agent YOUR_FAMILY)"
eval "$(agent-inbox claim)"
agent-inbox brief
```

Migrated 87 private cache files into the new domain's hashed credential directory
under `~/.agent-inboxes/hosted`, preserving keys with mode 0600. Original cached
files remain for temporary old-origin compatibility. Other Macs should use the
canonical URL and complete one-time hosted setup there if not already migrated.
Do not print or copy credentials into project documentation.

## Evidence

Inbox commit `377dc2c`, Worker version
`1d810f69-c054-44aa-8ce2-f5c904d17606`. Deployment config:
`cloudflare/wrangler.production.json`; the exact-host route takes precedence
over the existing wildcard app router.

- 163 Python unit tests passed with inherited hosted credentials removed from
  the test environment.
- Real Python Worker/Durable Object SQLite integration passed signed sessions,
  grant revocation, forged state, registrar and project scope, restart durability,
  and concurrent reservation rollback.
- 14 ai-startup tests passed; updated LaunchAgent plist validation passed.
- Production verified canonical owner/key access and history, anonymous API
  denial, East Bay cross-workspace key denial, and the legacy browser redirect.
- Browser verified real shared-SSO callback and Gmail identity in Inbox, Tasks,
  View, Share, and the main Suite; Inbox sign-out was also exercised.

Fresh biometric/passkey enrollment was not exercised. Browser acceptance used
Nate's existing authenticated hub session.

## Product acceptance

Tasks production commit `7df9aac` preserves all three original owner workspaces.
Both domains returned the same five projects with existing credentials. Navigation
and document titles use Nate’s Software on the canonical hostname and East Bay
Projects on the branded hostname; generated invitation/share links are canonical.

View retains the prior machine and folder-share records under the Gmail owner.
The main Suite preserves its existing `usr_nate` account and application data.
Share reads, raw text, preview cards and creation require Share admission.
Independent review found and closed legacy owner-token bypasses: existing owner
API credentials now require the hub's live entitlement too.

Public exceptions are authentication pages, bundled static distribution files,
and minimal operational health/configuration flags with no keys, account IDs or
customer data. They do not authorize product use. Fresh passkey enrollment and
new third-party invitations were not performed on production during validation.

## Final protected release and historical URLs

The Suite guarded release completed from `600ce2eb` at
`https://052a4386.nates-software.pages.dev`, including 103 tests, production
build, migration checks, private admission checks, native CLI distribution
checks and live payment/drain configuration verification. The operational
configuration exception is exact GET/HEAD only; application APIs stay private.

Retired 494 superseded Pages deployment artifacts without deleting D1 data,
R2 files or Git history. A complete HTTP audit found 492 returning 404, one
already protected preview returning 401, and one provider-retained hostname
still serving old code despite successful deletion. That exact hostname,
`f831fff3.nates-software.pages.dev`, now has an account Bulk Redirect (308) to
the canonical protected site, preserving path/query. Following its old API URL
returns 401. No Cloudflare Access app was introduced.

Redirect list: `1170b7eec46d443490db31b3a1b74c42`.
Redirect rule: `8b9da72e5e8e4ab0b52c53be8ec93308` in ruleset
`eb7294dccbce4180accc3c1449deafa5`. It matches only the retired exact hostname.
Current and future deployment hosts are unaffected. The provider's matching
known issue is https://github.com/cloudflare/developer-platform/issues/67.
Private retirement inventory/evidence lives under
`~/.local/state/nates-sso-migration/`; private pre-migration Inbox Access and
hub database backups also reside in this repository's ignored
`.agent-inboxes/migration-backups/2026-09-27/` directory.

Auth, View and Share workers.dev and version-preview URLs are explicitly
turned off. Inbox and Tasks already disable these URLs. Future releases must
retain the SSO gate; never restore an old pre-tenancy deployment to production.
