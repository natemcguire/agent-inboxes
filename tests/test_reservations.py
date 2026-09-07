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


class TestRepoKeysResourcesAndTakeoverMail(unittest.TestCase):
    """v1.3.0: worktree-safe repo keys, forced-takeover mail, resource leases."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.conn = get_connection(Path(self.tmp_dir.name) / "res13.db")
        self.svc = InboxService(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_repo_key_conflict_matrix(self):
        # Holder A reserves with repo key K1.
        self.svc.acquire_reservations(
            "proj", ["src/app.ts"], "a@proj", "s-aaaa",
            client_token=_tok(), repo_key="aaaaaaaaaaaa",
        )
        # Different repo key -> different repository sharing a basename: free.
        res_other_repo = self.svc.acquire_reservations(
            "proj", ["src/app.ts"], "b@proj", "s-bbbb",
            client_token=_tok(), repo_key="bbbbbbbbbbbb",
        )
        self.assertNotIn("conflicts", res_other_repo)

        # Same repo key -> conflicts with A's lease.
        res_same_repo = self.svc.acquire_reservations(
            "proj", ["src/app.ts"], "c@proj", "s-cccc",
            client_token=_tok(), repo_key="aaaaaaaaaaaa",
        )
        self.assertIn("conflicts", res_same_repo)

        # NULL requester key stays conservative: conflicts with BOTH keyed leases.
        res_null_req = self.svc.acquire_reservations(
            "proj", ["src/app.ts"], "d@proj", "s-dddd", client_token=_tok(),
        )
        self.assertIn("conflicts", res_null_req)
        self.assertEqual(len(res_null_req["conflicts"]), 2)

        # NULL row key stays conservative too: keyed requester on a keyless lease.
        self.svc.acquire_reservations(
            "proj", ["legacy.txt"], "d@proj", "s-dddd", client_token=_tok(),
        )
        res_keyed_vs_null = self.svc.acquire_reservations(
            "proj", ["legacy.txt"], "a@proj", "s-aaaa",
            client_token=_tok(), repo_key="aaaaaaaaaaaa",
        )
        self.assertIn("conflicts", res_keyed_vs_null)

    def test_forced_takeover_notifies_displaced_holder_and_isolates_mail_failure(self):
        self.svc.acquire_reservations(
            "proj", ["src/a.ts", "src/b.ts"], "victim@proj", "s-vvvv",
            reason="editing both", client_token=_tok(),
        )
        res = self.svc.acquire_reservations(
            "proj", ["src/a.ts", "src/b.ts"], "taker@proj", "s-tttt",
            reason="hotfix", force=True, client_token=_tok(),
        )
        self.assertNotIn("conflicts", res)
        self.assertEqual(res.get("notified"), ["victim@proj"])

        # Exactly ONE mail for the event, listing both paths, readable by the victim.
        threads = self.svc.list_threads("victim@proj", unread_only=True)
        self.assertEqual(len(threads), 1)
        self.assertIn("Reservation takeover: src/a.ts", threads[0]["subject"])
        detail = self.svc.get_thread("victim@proj", threads[0]["thread_id"])
        body = detail["emails"][0]["body_markdown"]
        self.assertIn("src/a.ts", body)
        self.assertIn("src/b.ts", body)
        self.assertIn("forced:taker@proj", body)
        self.assertEqual(detail["emails"][0]["from"], "taker@proj")

        # Mail failure must not error the acquire nor roll back the takeover.
        self.svc.acquire_reservations(
            "proj", ["solo.ts"], "victim2@proj", "s-v2v2", client_token=_tok(),
        )
        with mock.patch.object(
            InboxService, "send_email", side_effect=RuntimeError("mail down")
        ):
            res2 = self.svc.acquire_reservations(
                "proj", ["solo.ts"], "taker@proj", "s-tttt",
                force=True, client_token=_tok(),
            )
        self.assertNotIn("conflicts", res2)
        self.assertNotIn("notified", res2)
        forced_row = self.conn.execute(
            "SELECT released_by FROM reservations WHERE path='solo.ts' AND released_at IS NOT NULL"
        ).fetchone()
        self.assertEqual(forced_row["released_by"], "forced:taker@proj")

    def test_resource_leases_exact_match_only_and_repo_key_exempt(self):
        res = self.svc.acquire_reservations(
            "proj", None, "a@proj", "s-aaaa",
            client_token=_tok(), resources=["release:pages"],
        )
        got = res["reservations"][0]
        self.assertEqual(got["path"], "release:pages")
        self.assertEqual(got["kind"], "resource")

        # Exact-match only: a longer-named resource does not prefix-conflict.
        res2 = self.svc.acquire_reservations(
            "proj", None, "b@proj", "s-bbbb",
            client_token=_tok(), resources=["release:pages-preview"],
        )
        self.assertNotIn("conflicts", res2)

        # Same resource name conflicts even with different repo keys (project-wide).
        res3 = self.svc.acquire_reservations(
            "proj", None, "c@proj", "s-cccc",
            client_token=_tok(), resources=["release:pages"], repo_key="cccccccccccc",
        )
        self.assertIn("conflicts", res3)
        self.assertEqual(res3["conflicts"][0]["kind"], "resource")

        # A file path can never collide with the resource namespace.
        res4 = self.svc.acquire_reservations(
            "proj", ["res://release:pages"], "d@proj", "s-dddd", client_token=_tok(),
        )
        # Explicit res:// in a path list round-trips to the same resource -> conflict,
        # while an ordinary file named like the resource is free.
        self.assertIn("conflicts", res4)
        res5 = self.svc.acquire_reservations(
            "proj", ["release:pages"], "d@proj", "s-dddd", client_token=_tok(),
        )
        self.assertNotIn("conflicts", res5)

        # Invalid resource names are rejected.
        with self.assertRaises(service_mod.ValidationError):
            self.svc.acquire_reservations(
                "proj", None, "e@proj", "s-eeee",
                client_token=_tok(), resources=["Bad Name!"],
            )

    def test_global_listing_and_history_ordering_limit(self):
        real_now = utc_now_iso()
        self.svc.acquire_reservations(
            "proj-one", ["a.txt"], "a@proj-one", "s-aaaa", client_token=_tok(), ttl_seconds=60,
        )
        self.svc.acquire_reservations(
            "proj-two", None, "b@proj-two", "s-bbbb",
            client_token=_tok(), resources=["release:pages"],
        )
        self.svc.release_reservations("proj-one", "a@proj-one", "s-aaaa", release_all=True)
        # A second finished row, later: forced takeover in proj-two.
        self.svc.acquire_reservations(
            "proj-two", ["z.txt"], "b@proj-two", "s-bbbb", client_token=_tok(), ttl_seconds=60,
        )
        # Advance past the 60s lease's expiry but inside the resource's 15m TTL.
        future = _iso_add_seconds(real_now, 120)
        with mock.patch.object(service_mod, "utc_now_iso", return_value=future):
            # Sweep on the global read: z.txt expires into history.
            active = self.svc.list_reservations_all()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["project"], "proj-two")
            self.assertEqual(active[0]["kind"], "resource")

            history = self.svc.reservation_history(None, limit=50)
            # Newest first: expiry of z.txt (released_at = its expires_at,
            # 60s after creation) sorts after a.txt's immediate release.
            self.assertEqual([h["path"] for h in history], ["z.txt", "a.txt"])
            self.assertEqual(history[0]["released_by"], "expired")
            self.assertEqual(history[0]["project"], "proj-two")
            self.assertEqual(history[1]["released_by"], "holder")
            self.assertEqual(history[1]["project"], "proj-one")
            self.assertEqual(len(self.svc.reservation_history(None, limit=1)), 1)
            # Per-project history filters correctly.
            per = self.svc.reservation_history("proj-one", limit=50)
            self.assertEqual([h["path"] for h in per], ["a.txt"])
