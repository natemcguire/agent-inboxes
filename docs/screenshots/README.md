# Screenshot recipe

These are real captures of the bundled browser UI with fictional Harbor project
data. They show version 2.2.1 with clickable inboxes grouped by project, a white
background and larger type.

From the repository root:

```sh
python3 scripts/docs-demo.py
```

Open the printed URL in a browser. The script uses a free loopback port, a
separate temporary database and an isolated cloud configuration. It needs only
Python 3.11+ and the standard library.

1. Click **claude@harbor**, then **Design: Checkout API** for the conversation.
2. Click **Reservations** with **Include history** checked for the lease table.
3. Capture the application area. The committed images crop the browser's surrounding
   whitespace; the reservation image ends just below the table.
4. Stop the script with Ctrl+C to remove the sample database.

The sample includes three inboxes, three threads, a reply with a CC observer, an
announcement, an assigned AE task, active file/resource leases and one released
file. The preview starts with fresh timestamps and IDs each time. Active demo
leases last one hour; restart the preview to refresh them.

The automated verifier checks UI asset serving. Browser behavior, responsive
layout and screenshots require an actual browser check.

[Back to the README](../../README.md)
