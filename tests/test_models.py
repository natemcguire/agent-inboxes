"""Unit tests for models, address parsing, validation, IDs, and timestamps."""

import re
import unittest
from agent_inbox.models import (
    ConflictError,
    InboxError,
    NotFoundError,
    ServerNotRunningError,
    ValidationError,
    generate_email_id,
    generate_thread_id,
    is_valid_address,
    is_valid_slug,
    normalize_address,
    normalize_slug,
    parse_address,
    utc_now_iso,
)


class TestModels(unittest.TestCase):

    def test_slug_validation(self):
        self.assertTrue(is_valid_slug("boats"))
        self.assertTrue(is_valid_slug("nate-bot"))
        self.assertTrue(is_valid_slug("worker-1"))
        self.assertTrue(is_valid_slug("a"))
        self.assertTrue(is_valid_slug("0"))

        self.assertFalse(is_valid_slug("-leading"))
        self.assertFalse(is_valid_slug("has spaces"))
        self.assertFalse(is_valid_slug("has_underscore"))
        self.assertFalse(is_valid_slug("has.dot"))
        self.assertFalse(is_valid_slug(""))
        self.assertFalse(is_valid_slug("a" * 64))  # Max 63 chars

    def test_address_validation(self):
        self.assertTrue(is_valid_address("claude@nate-bot"))
        self.assertTrue(is_valid_address("codex-worker1@boats"))
        self.assertTrue(is_valid_address("orca@orca-cluster"))

        self.assertFalse(is_valid_address("bad-address"))
        self.assertFalse(is_valid_address("@project"))
        self.assertFalse(is_valid_address("agent@"))
        self.assertFalse(is_valid_address("agent@-badproject"))
        self.assertFalse(is_valid_address("-badagent@project"))

    def test_normalize_slug(self):
        self.assertEqual(normalize_slug("My_Cool_Project.git"), "my-cool-project-git")
        self.assertEqual(normalize_slug("  Boats  "), "boats")
        self.assertEqual(normalize_slug("123-repo"), "123-repo")
        self.assertEqual(normalize_slug(""), "unknown")
        # Ensure it doesn't start with dash
        self.assertEqual(normalize_slug("---leading-dashes"), "leading-dashes")

    def test_normalize_and_parse_address(self):
        self.assertEqual(normalize_address("Claude@Nate-Bot"), "claude@nate-bot")
        local, project = parse_address("Codex-Worker1@Boats")
        self.assertEqual(local, "codex-worker1")
        self.assertEqual(project, "boats")

        with self.assertRaises(ValidationError):
            parse_address("invalid_address_format")

        with self.assertRaises(ValidationError):
            parse_address("")

    def test_id_generators(self):
        t1 = generate_thread_id()
        t2 = generate_thread_id()
        self.assertTrue(t1.startswith("thr_"))
        self.assertTrue(t2.startswith("thr_"))
        self.assertNotEqual(t1, t2)

        e1 = generate_email_id()
        e2 = generate_email_id()
        self.assertTrue(e1.startswith("eml_"))
        self.assertTrue(e2.startswith("eml_"))
        self.assertNotEqual(e1, e2)

    def test_utc_now_iso_format(self):
        ts = utc_now_iso()
        # ISO RFC 3339 with Z, e.g. 2026-08-30T16:56:28.123Z
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertTrue(pattern.match(ts), f"Timestamp '{ts}' did not match RFC 3339 pattern")

    def test_exceptions_and_error_envelopes(self):
        err = ValidationError("invalid_field", "Field is bad")
        self.assertEqual(err.status_code, 400)
        self.assertEqual(err.to_dict(), {
            "error": {
                "code": "invalid_field",
                "message": "Field is bad",
            }
        })

        nf = NotFoundError("not_found", "Thread not found")
        self.assertEqual(nf.status_code, 404)

        cf = ConflictError("duplicate_entry", "Already exists")
        self.assertEqual(cf.status_code, 409)

        snr = ServerNotRunningError()
        self.assertEqual(snr.status_code, 503)
        self.assertEqual(snr.code, "server_not_running")


if __name__ == "__main__":
    unittest.main()
