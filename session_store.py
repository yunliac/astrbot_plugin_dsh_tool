"""Local session metadata and bounded fallback transcripts (stdlib only).

``long_term`` is a durable user label, not a reclaim exemption. Only ``exempt``
protects against idle reclaim. Remote refreshes never change existing titles or
user flags. Invalid stores fail closed: the original file is never rewritten.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any


class SessionStore:
    SCHEMA = 1
    MAX_TRANSCRIPT_ENTRIES = 1000
    MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
    _TEXT_FIELDS = frozenset({"origin", "channel", "title", "cwd", "status"})
    _REMOTE_FIELDS = frozenset({"origin", "channel", "cwd", "status", "last_used", "remote_seen_at"})
    _FIELDS = frozenset({"id", "umo", "session_id", "base", "origin", "channel", "title", "cwd", "status", "last_used", "long_term", "exempt", "updated_at", "remote_seen_at"})

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._transcripts: dict[str, list[dict[str, Any]]] = {}
        if self.path is not None:
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                pass
            else:
                # Parsing/validation errors deliberately propagate, preserving bad data.
                self._load(json.loads(raw, object_pairs_hook=self._unique_object))

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    @staticmethod
    def record_id(umo: str, base: str) -> str:
        return hashlib.sha256((base + "\n" + umo).encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _text(value: Any, name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        return value

    @staticmethod
    def _number(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite Unix timestamp")
        return value

    @staticmethod
    def _encoded(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")

    def _load(self, data: Any) -> None:
        if not isinstance(data, dict) or set(data) != {"schema", "records", "transcripts"}:
            raise ValueError("Invalid session store envelope")
        if type(data["schema"]) is not int or data["schema"] != self.SCHEMA:
            raise ValueError("Unsupported session store schema")
        records, transcripts = data["records"], data["transcripts"]
        if not isinstance(records, dict) or not isinstance(transcripts, dict):
            raise ValueError("Invalid session store collections")
        for rid, row in records.items():
            if not isinstance(row, dict) or set(row) != self._FIELDS:
                raise ValueError("Invalid session record fields")
            for name in self._TEXT_FIELDS | {"id", "umo", "session_id", "base"}:
                self._text(row[name], name)
            if rid != row["id"] or rid != self.record_id(row["umo"], row["base"]):
                raise ValueError("Session record ID does not match its lane")
            for name in ("long_term", "exempt"):
                if type(row[name]) is not bool:
                    raise ValueError(f"Invalid {name} flag")
            for name in ("last_used", "updated_at", "remote_seen_at"):
                self._number(row[name], name)
        count = 0
        for rid, entries in transcripts.items():
            if rid not in records or not isinstance(entries, list):
                raise ValueError("Invalid transcript owner or entries")
            count += len(entries)
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {"task", "result", "at"}:
                    raise ValueError("Invalid transcript entry")
                self._text(entry["task"], "task")
                self._text(entry["result"], "result")
                self._number(entry["at"], "at")
        if count > self.MAX_TRANSCRIPT_ENTRIES or len(self._encoded(transcripts)) > self.MAX_TRANSCRIPT_BYTES:
            raise ValueError("Transcript limits exceeded")
        self._records, self._transcripts = records, transcripts

    def _persist(self) -> None:
        if self.path is None:
            return
        payload = self._encoded({"schema": self.SCHEMA, "records": self._records, "transcripts": self._transcripts})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _commit(self, operation):
        # Failed writes roll back memory as well as preserving the previous file.
        with self._lock:
            previous = copy.deepcopy((self._records, self._transcripts))
            try:
                result = operation()
                self._persist()
            except Exception:
                self._records, self._transcripts = previous
                raise
            return copy.deepcopy(result)

    def upsert(self, umo: str, session_id: str, base: str, **safe_fields) -> dict:
        for name, value in (("umo", umo), ("session_id", session_id), ("base", base)):
            self._text(value, name)
        # An explicit allowlist means credentials and arbitrary remote metadata
        # are never retained. Flags are accepted only through set_flags().
        fields = {key: value for key, value in safe_fields.items() if key in self._REMOTE_FIELDS}
        for key, value in fields.items():
            if key in self._TEXT_FIELDS:
                self._text(value, key)
            else:
                self._number(value, key)
        if "title" in safe_fields:
            self._text(safe_fields["title"], "title")
        rid = self.record_id(umo, base)

        def mutate():
            now = time.time()
            row = self._records.get(rid)
            if row is None:
                row = {"id": rid, "umo": umo, "session_id": session_id, "base": base,
                       "origin": "", "channel": "", "title": safe_fields.get("title", ""),
                       "cwd": "", "status": "active", "last_used": now,
                       "long_term": False, "exempt": False, "updated_at": now, "remote_seen_at": now}
                self._records[rid] = row
            row.update(session_id=session_id, last_used=now, remote_seen_at=now, updated_at=now)
            row.update(fields)
            return row
        return self._commit(mutate)

    def get(self, record_id: str) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._records.get(record_id))

    def find(self, umo: str, base: str | None = None) -> dict | None:
        """Find an exact lane, or the most recently used lane across bases."""
        with self._lock:
            rows = [row for row in self._records.values() if row["umo"] == umo and (base is None or row["base"] == base)]
            return copy.deepcopy(max(rows, key=lambda row: row["last_used"])) if rows else None

    def list(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(list(self._records.values()))

    def set_flags(self, record_id: str, long_term: bool | None = None,
                  exempt: bool | None = None, title: str | None = None) -> dict:
        for name, value in (("long_term", long_term), ("exempt", exempt)):
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be bool or None")
        if title is not None:
            self._text(title, "title")

        def mutate():
            row = self._records[record_id]
            for name, value in (("long_term", long_term), ("exempt", exempt), ("title", title)):
                if value is not None:
                    row[name] = value
            row["updated_at"] = time.time()
            return row
        return self._commit(mutate)

    def is_exempt(self, umo: str, session_id: str | None = None) -> bool:
        with self._lock:
            lanes = [row for row in self._records.values() if row["umo"] == umo]
            sids = {session_id} if session_id else {row["session_id"] for row in lanes if row["session_id"]}
            return any(row["exempt"] and (row["umo"] == umo or row["session_id"] in sids)
                       for row in self._records.values())

    def mark_status(self, id: str, status: str) -> dict:
        self._text(status, "status")

        def mutate():
            row = self._records[id]
            row.update(status=status, updated_at=time.time())
            return row
        return self._commit(mutate)

    def append_exchange(self, record_id: str, task: str, result: str,
                        max_total_bytes: int | None = None) -> None:
        """Append one task/result pair; evict oldest pairs across all lanes.

        The global transcript budget is 1000 pairs / 2 MiB of UTF-8 JSON.
        A smaller byte budget can be requested. A single oversized pair is
        truncated (both strings proportionally), retaining the latest exchange.
        """
        self._text(task, "task")
        self._text(result, "result")
        budget = self.MAX_TRANSCRIPT_BYTES if max_total_bytes is None else max_total_bytes
        if type(budget) is not int or budget < 256:
            raise ValueError("max_total_bytes must be an integer >= 256")
        budget = min(budget, self.MAX_TRANSCRIPT_BYTES)

        def mutate():
            self._records[record_id]  # Unknown IDs must not create orphan transcripts.
            entry = {"task": task, "result": result, "at": time.time()}
            # Binary search by character count; JSON bytes, including escaping,
            # are measured, so non-ASCII and control characters obey the cap.
            if len(self._encoded({record_id: [entry]})) > budget:
                low, high = 0, max(len(task), len(result))
                while low < high:
                    mid = (low + high + 1) // 2
                    candidate = dict(entry, task=task[:mid], result=result[:mid])
                    if len(self._encoded({record_id: [candidate]})) <= budget:
                        low = mid
                    else:
                        high = mid - 1
                entry.update(task=task[:low], result=result[:low])
            self._transcripts.setdefault(record_id, []).append(entry)
            while (sum(map(len, self._transcripts.values())) > self.MAX_TRANSCRIPT_ENTRIES
                   or len(self._encoded(self._transcripts)) > budget):
                oldest = min(self._transcripts, key=lambda rid: self._transcripts[rid][0]["at"])
                self._transcripts[oldest].pop(0)
                if not self._transcripts[oldest]:
                    del self._transcripts[oldest]
            self._records[record_id]["updated_at"] = time.time()
        self._commit(mutate)

    def transcript(self, id: str) -> list:
        with self._lock:
            return copy.deepcopy(self._transcripts.get(id, []))
