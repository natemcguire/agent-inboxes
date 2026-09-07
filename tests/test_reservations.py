"""Focused tests for the NB-7 file reservation system (spec test list)."""

import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import agent_inbox.service as service_mod
from agent_inbox.client import InboxClient
from agent_inbox.db import get_connection
from agent_inbox.server import AgentInboxServer
from agent_inbox.service import InboxService, _iso_add_seconds, utc_now_iso


def _tok() -> str:
    return str(uuid.uuid4())


class TestReservationSemantics(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.conn = get_connection(Path(self.tmp_dir.name) / "res.db")
        self.svc = InboxService(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_acquire_conflict_all_or_nothing_prefix_and_idempotent_reacquire(self):
        # Agent A reserves a file and a directory.
        res = self.svc.acquire_reservations(
            "proj", ["src/lib/money.ts", "docs/"], "a@proj", "s-aaaa", "money work", client_token=_tok()
        )
        self.assertEqual({r["path"] for r in res["reservations"]}, {"src/lib/money.ts", "docs/"})

        # Agent B: multi-path acquire where ONE path conflicts -> all-or-nothing.
        res_b = self.svc.acquire_reservations(
            "proj",
            ["free/file.ts", "SRC/LIB/MONEY.TS"],  # case-insensitive exact conflict
            "b@proj", "s-bbbb", client_token=_tok(),
        )
        self.assertIn("conflicts", res_b)
        self.assertEqual(res_b["conflicts"][0]["holder"], "a@proj")
        # Nothing was reserved, including the free path.
        active = self.svc.list_reservations("proj")
        self.assertEqual({r["path"] for r in active}, {"src/lib/money.ts", "docs/"})

        # Prefix conflicts both directions: dir blocks descendant file...
        res_desc = self.svc.acquire_reservations(
            "proj", ["docs/readme.md"], "b@proj", "s-bbbb", client_token=_tok()
        )
        self.assertIn("conflicts", res_desc)
        # ...and a held file blocks reserving its ancestor directory.
        res_anc = self.svc.acquire_reservations(
            "proj", ["src/lib/"], "b@proj", "s-bbbb", client_token=_tok()
        )
        self.assertIn("conflicts", res_anc)

        # Own re-acquire (same address+session) is idempotent and renews.
        before = next(r for r in active if r["path"] == "src/lib/money.ts")["expires_at"]
        res_again = self.svc.acquire_reservations(
            "proj", ["src/lib/money.ts"], "a@proj", "s-aaaa", client_token=_tok()
        )
        self.assertNotIn("conflicts", res_again)
        after = res_again["reservations"][0]["expires_at"]
        self.assertGreaterEqual(after, before)
        # Same session in a DIFFERENT session slug counts as a different holder.
        res_other_sess = self.svc.acquire_reservations(
            "proj", ["src/lib/money.ts"], "a@proj", "s-cccc", client_token=_tok()
        )
        self.assertIn("conflicts", res_other_sess)

    def test_expiry_lazy_sweep_and_takeover_audit(self):
        real_now = utc_now_iso()
        self.svc.acquire_reservations(
            "proj", ["a.txt"], "a@proj", "s-aaaa", ttl_seconds=60, client_token=_tok()
        )
        # Force one active lease to be taken over.
        self.svc.acquire_reservations(
            "proj", ["a.txt"], "b@proj", "s-bbbb", force=True, client_token=_tok()
        )
        forced = self.conn.execute(
            "SELECT released_by FROM reservations WHERE released_at IS NOT NULL"
        ).fetchone()
        self.assertEqual(forced["released_by"], "forced:b@proj")

        # Advance the clock past b's expiry (clock injection — no sleeps).
        future = _iso_add_seconds(real_now, 3 * 60 * 60)
        with mock.patch.object(service_mod, "utc_now_iso", return_value=future):
            # Any touch sweeps: the expired lease is auto-released...
            self.assertEqual(self.svc.list_reservations("proj"), [])
            expired = self.conn.execute(
                "SELECT released_by FROM reservations WHERE released_by = 'expired'"
            ).fetchall()
            self.assertEqual(len(expired), 1)
            # ...and a fresh acquire on the same path simply succeeds.
            res = self.svc.acquire_reservations(
                "proj", ["a.txt"], "c@proj", "s-cccc", client_token=_tok()
            )
            self.assertNotIn("conflicts", res)

    def test_concurrent_acquire_exactly_one_wins(self):
        db_path = Path(self.tmp_dir.name) / "res.db"
        results = []
        barrier = threading.Barrier(2)

        def try_acquire(who):
            conn = get_connection(db_path)
            try:
                svc = InboxService(conn)
                barrier.wait()
                res = svc.acquire_reservations(
                    "proj", ["hot/path.ts"], f"{who}@proj", f"s-{who}", client_token=_tok()
                )
                results.append((who, "conflicts" not in res))
            finally:
                conn.close()

        threads = [threading.Thread(target=try_acquire, args=(w,)) for w in ("t1", "t2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wins = [who for who, won in results if won]
        self.assertEqual(len(wins), 1, f"exactly one acquirer must win, got {results}")


class TestReservationWaitE2E(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_conn = get_connection(Path(self.tmp_dir.name) / "wait.db")
        self.server = AgentInboxServer(("127.0.0.1", 0), self.db_conn, verbose=False)
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db_conn.close()
        self.tmp_dir.cleanup()

    def test_wait_unblocks_promptly_on_release(self):
        holder = InboxClient(self.base_url, session_id="s-hold0001")
        waiter = InboxClient(self.base_url, session_id="s-wait0001")

        holder.acquire_reservations("proj", ["src/app.ts"], "holder@proj", reason="editing")

        def release_later():
            time.sleep(0.6)
            holder.release_reservations("proj", "holder@proj", release_all=True)

        t = threading.Thread(target=release_later, daemon=True)
        started = time.monotonic()
        t.start()
        res = waiter.wait_reservations("proj", ["src/app.ts"], "waiter@proj", timeout=10)
        elapsed = time.monotonic() - started
        t.join()

        self.assertTrue(res["free"])
        self.assertLess(elapsed, 5.0)  # unblocked well before the 10s timeout

        # And the waiter can now actually acquire.
        acq = waiter.acquire_reservations("proj", ["src/app.ts"], "waiter@proj")
        self.assertEqual(acq["reservations"][0]["path"], "src/app.ts")


if __name__ == "__main__":
    unittest.main()
