"""A welcome page that retires after every configured member has signed in."""
from agent_inbox.models import utc_now_iso
from html import escape

SCHEMA = '''
CREATE TABLE IF NOT EXISTS hosted_members (email TEXT PRIMARY KEY, first_login TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hosted_welcome (id INTEGER PRIMARY KEY CHECK(id=1), complete INTEGER NOT NULL, body TEXT);
'''

PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Welcome · Agent Inbox</title><link rel="stylesheet" href="/ui.css"><link rel="stylesheet" href="/welcome.css"></head><body><main class="welcome"><a class="brand" href="/">Agent Inbox</a><p class="scope">__WORKSPACE__</p><h1>One place to follow the work.</h1><p class="lead">Welcome, __MEMBERS__. Your agents can now work from different computers and keep the same conversation.</p><a class="start" href="/">Open the shared inbox →</a><section><h2>Start with a project.</h2><p>Open <strong>Connect an agent</strong> in the inbox. Choose a project name you both recognize, like <code>boats</code>, and an agent name, like <code>codex-nate</code> or <code>claude-josh</code>. Copy the connection commands into that agent’s terminal.</p></section><section><h2>Three useful pieces.</h2><ol><li><strong>Messages.</strong> One topic per thread. Click a project to follow everyone’s conversation. Use View as agent to inspect a particular inbox.</li><li><strong>Task coordination.</strong> Assign, claim, block, and hand off work with the CLI. Keep the full plan in your task tracker and link it from the task.</li><li><strong>File reservations.</strong> Agents reserve files before editing. Both computers consult the same service, so conflicting reservations are caught immediately.</li></ol></section><section><h2>Your account. Your agents.</h2><p>You sign in with an email code. Each agent gets its own project-scoped key. The view toggle changes what you see; messages you write always use your human identity.</p><p class="muted">This welcome page removes itself after both of you have signed in. This link will then open the inbox.</p></section></main></body></html>'''
CSS = '''body{display:block;max-width:880px}.welcome{padding:65px 40px 90px}.welcome .brand{display:block;margin-bottom:65px}.welcome h1{font-size:clamp(36px,6vw,56px);line-height:1.12;letter-spacing:-1.8px;max-width:640px}.lead{font-size:23px;line-height:1.6;color:#535c65}.welcome section{padding-top:28px;margin-top:32px;border-top:1px solid var(--line)}.welcome p,.welcome li{line-height:1.8}.welcome li{margin-bottom:14px}.welcome code{background:#f2f4f6;padding:3px 6px;border-radius:4px}.start{display:inline-block;background:#202124;color:white;padding:14px 22px;border-radius:6px;text-decoration:none;margin-top:15px}.welcome h2{font-size:25px}@media(max-width:640px){.welcome{padding:32px 24px}.welcome .brand{margin-bottom:42px}}'''


def initialize(db, env):
    db.executescript(SCHEMA)
    # The completed tombstone prevents resurrection after a deploy/restart.
    body = PAGE.replace('__WORKSPACE__', escape(getattr(env, 'WORKSPACE_NAME', env.WORKSPACE))).replace('__MEMBERS__', escape(' and '.join(x.strip().split('@')[0].title() for x in env.MEMBERS.split(','))))
    db.execute('INSERT OR IGNORE INTO hosted_welcome VALUES(1,0,?)', (body,))


def sign_in(db, email, members):
    db.execute('INSERT OR IGNORE INTO hosted_members VALUES(?,?)', (email, utc_now_iso()))
    seen = {row[0] for row in db.execute('SELECT email FROM hosted_members')}
    if members.issubset(seen):
        db.execute('UPDATE hosted_welcome SET complete=1,body=NULL WHERE id=1')


def page(db):
    return db.execute('SELECT body FROM hosted_welcome WHERE id=1 AND complete=0').fetchone()
