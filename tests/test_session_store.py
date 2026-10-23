"""Standalone stdlib tests: python -m unittest discover -s tests -p test_session_store.py."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location("session_store", Path(__file__).resolve().parents[1] / "session_store.py")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
SessionStore = _MODULE.SessionStore


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sessions.json"

    def test_reload_flags_title_and_transcript(self):
        store = SessionStore(self.path)
        row = store.upsert("umo", "sid", "https://dsh", title="remote", cwd="/tmp")
        store.set_flags(row["id"], long_term=True, exempt=True, title="my note")
        store.append_exchange(row["id"], "task", "result")
        store.upsert("umo", "new-sid", "https://dsh", long_term=False, exempt=False, title="overwritten", token="SECRET", api_token="SECRET")
        loaded = SessionStore(self.path)
        row2 = loaded.get(row["id"])
        self.assertTrue(row2["long_term"])
        self.assertTrue(row2["exempt"])
        self.assertEqual(row2["title"], "my note")
        self.assertEqual(row2["session_id"], "new-sid")
        self.assertEqual(loaded.transcript(row["id"])[0]["result"], "result")
        self.assertNotIn("SECRET", self.path.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(self.path.read_text())["schema"], 1)

    def test_stable_id_and_independent_bases(self):
        store = SessionStore(None)
        row = store.upsert("umo", "sid", "base", origin="lane", channel="channel")
        expected = hashlib.sha256(b"base\numo").hexdigest()[:24]
        self.assertEqual(row["id"], expected)
        store.set_flags(expected, True, True, "keep")
        row2 = store.upsert("umo", "sid2", "base")
        self.assertEqual(row2["id"], expected)
        self.assertEqual(row2["origin"], "lane")
        self.assertEqual(row2["channel"], "channel")
        self.assertTrue(row2["exempt"])
        other = store.upsert("umo", "sid3", "other")
        self.assertNotEqual(other["id"], expected)
        self.assertEqual(store.find("umo", "base")["id"], expected)
        self.assertEqual(store.find("umo")["id"], other["id"])
        self.assertIsNone(store.find("missing"))

    def test_alias_exemption_and_long_term_independence(self):
        store = SessionStore(None)
        first = store.upsert("first", "same-sid", "base")
        store.upsert("second", "same-sid", "base")
        store.set_flags(first["id"], long_term=True)
        self.assertFalse(store.is_exempt("first"))
        self.assertFalse(store.is_exempt("second", "same-sid"))
        store.set_flags(first["id"], exempt=True)
        self.assertTrue(store.is_exempt("second"))
        self.assertTrue(store.is_exempt("unknown", "same-sid"))
        self.assertFalse(store.is_exempt("unknown", "different"))
        store.set_flags(first["id"], exempt=False)
        self.assertFalse(store.is_exempt("second"))
        self.assertTrue(store.get(first["id"])["long_term"])

    def test_results_are_defensive_copies(self):
        store = SessionStore(None)
        row = store.upsert("u", "s", "b")
        rid = row["id"]
        row["exempt"] = True
        store.get(rid)["exempt"] = True
        store.find("u")["exempt"] = True
        store.list()[0]["exempt"] = True
        changed = store.set_flags(rid, title="note")
        changed["title"] = "bad"
        status = store.mark_status(rid, "idle")
        status["status"] = "bad"
        store.append_exchange(rid, "task", "result")
        transcript = store.transcript(rid)
        transcript[0]["result"] = "bad"
        transcript.append({})
        self.assertFalse(store.is_exempt("u"))
        self.assertEqual(store.get(rid)["title"], "note")
        self.assertEqual(store.get(rid)["status"], "idle")
        self.assertEqual(store.transcript(rid)[0]["result"], "result")
        self.assertEqual(len(store.transcript(rid)), 1)

    def test_atomic_replace_and_failed_write_rollback(self):
        store = SessionStore(self.path)
        row = store.upsert("u", "s", "b")
        old_bytes = self.path.read_bytes()
        replace = os.replace
        observations = []

        def inspect_replace(source, target):
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(Path(target), self.path)
            self.assertEqual(self.path.read_bytes(), old_bytes)
            self.assertTrue(json.loads(Path(source).read_text())["records"][row["id"]]["exempt"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
            observations.append(True)
            replace(source, target)

        with patch.object(_MODULE.os, "replace", side_effect=inspect_replace):
            store.set_flags(row["id"], exempt=True)
        self.assertEqual(observations, [True])
        before_failure = self.path.read_bytes()
        with patch.object(_MODULE.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                store.set_flags(row["id"], exempt=False)
        self.assertEqual(self.path.read_bytes(), before_failure)
        self.assertTrue(store.get(row["id"])["exempt"])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_malformed_stores_preserved(self):
        valid_store = SessionStore(None)
        row = valid_store.upsert("u", "s", "b")
        valid = {"schema": 1, "records": {row["id"]: row}, "transcripts": {}}
        bad_flag = json.loads(json.dumps(valid))
        bad_flag["records"][row["id"]]["exempt"] = "false"
        bad_id = json.loads(json.dumps(valid))
        bad_id["records"][row["id"]]["umo"] = "changed"
        cases = ["{broken", "null", "{}", '{"schema":1,"schema":1}',
                 json.dumps(dict(valid, schema=2)), json.dumps(bad_flag), json.dumps(bad_id),
                 json.dumps(dict(valid, transcripts={"orphan": []}))]
        for raw in cases:
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                with self.assertRaises((ValueError, TypeError)):
                    SessionStore(self.path)
                self.assertEqual(self.path.read_text(encoding="utf-8"), raw)
        self.path.write_bytes(b"\xff\xfe")
        with self.assertRaises(UnicodeError):
            SessionStore(self.path)
        self.assertEqual(self.path.read_bytes(), b"\xff\xfe")

    def test_transcript_count_limit_and_reload(self):
        store = SessionStore(None)
        first = store.upsert("one", "s", "b")["id"]
        second = store.upsert("two", "s", "b")["id"]
        for index in range(1005):
            store.append_exchange(first if index % 2 == 0 else second, str(index), "ok")
        entries = store.transcript(first) + store.transcript(second)
        self.assertEqual(len(entries), 1000)
        self.assertEqual(min(int(entry["task"]) for entry in entries), 5)
        store.path = self.path
        store.mark_status(first, "idle")
        self.assertEqual(len(SessionStore(self.path).transcript(first)), 500)

    def test_transcript_byte_cap_handles_utf8_and_escaping(self):
        store = SessionStore(self.path)
        rid = store.upsert("u", "s", "b")["id"]
        store.append_exchange(rid, "old", "old")
        store.append_exchange(rid, "汉😀\n" * 300000, "result" * 500000)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        size = len(SessionStore._encoded(data["transcripts"]))
        self.assertLessEqual(size, 2 * 1024 * 1024)
        self.assertTrue(store.transcript(rid))
        self.assertNotEqual(store.transcript(rid)[-1]["task"], "old")
        self.assertEqual(SessionStore(self.path).transcript(rid), store.transcript(rid))
        store.append_exchange(rid, "\x00汉" * 500, "😀" * 500, max_total_bytes=512)
        self.assertLessEqual(len(SessionStore._encoded(store._transcripts)), 512)

    def test_invalid_inputs_do_not_change_state(self):
        store = SessionStore(None)
        row = store.upsert("u", "s", "b")
        before = store.list()
        for operation in (lambda: store.set_flags(row["id"], exempt="false"),
                          lambda: store.upsert("u", "s", "b", last_used=float("nan")),
                          lambda: store.append_exchange(row["id"], "task", "result", 1)):
            with self.assertRaises((ValueError, TypeError)):
                operation()
            self.assertEqual(store.list(), before)
        for operation in (lambda: store.set_flags("unknown", exempt=True),
                          lambda: store.mark_status("unknown", "idle"),
                          lambda: store.append_exchange("unknown", "task", "result")):
            with self.assertRaises(KeyError):
                operation()
        self.assertIsNone(store.get("unknown"))
        self.assertEqual(store.transcript("unknown"), [])


if __name__ == "__main__":
    unittest.main()
