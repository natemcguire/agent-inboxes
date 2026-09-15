# Screenshot recipe

These are real captures of the bundled browser UI with fictional Harbor project
data. They show the 2.3 UI with human project observation, individual agent
inboxes, a white background and larger type. The human capture includes the
section-heading fix from 2.3.1.

From the repository root:

```sh
python3 scripts/docs-demo.py
```

Open the printed URL in a browser. The script uses a free loopback port, a
separate temporary database and an isolated cloud configuration. It needs only
Python 3.11+ and the standard library.

1. Click **harbor**, expand **Design: Checkout API**, then **Expand messages**
   for [the human view](conversation.png).
2. Toggle **View as agent** and expand the latest reply's **Message details**
   for [the agent view](agent-view.png).
3. Click **Reservations** with **Include history** checked for
   [the lease table](reservations.png).
4. Capture the application area from the top of the page. The committed images
   crop the surrounding whitespace and end below the featured conversation/table.
5. Stop the script with Ctrl+C to remove the sample database.

The sample includes three inboxes, three threads, a reply with a CC observer, an
announcement, an assigned AE task, active file/resource leases and one released
file. The preview starts with fresh timestamps and IDs each time. Active demo
leases last one hour; restart the preview to refresh them.
The seed writes directly to SQLite, so it has no API peer IP. Actual HTTP
sends/replies record the service's observed connection address.

The automated verifier checks UI asset serving. Browser behavior, responsive
layout and screenshots require an actual browser check.

[Back to the README](../../README.md)
