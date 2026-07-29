from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from uuid import uuid4


SCHEMA_VERSION = 1
_BOOK_LOCK = RLock()


class PortfolioBookStore:
    """Atomic canonical storage with legacy JSON files kept as mirrors."""

    def __init__(
        self,
        book_path: Path,
        state_path: Path,
        trade_path: Path,
        snapshot_path: Path,
    ) -> None:
        self.book_path = book_path
        self.state_path = state_path
        self.trade_path = trade_path
        self.snapshot_path = snapshot_path

    def load(self, default_state: dict[str, Any]) -> dict[str, Any]:
        with _BOOK_LOCK:
            book = self._load_unlocked(default_state)
            return deepcopy(book)

    def update(
        self,
        default_state: dict[str, Any],
        mutator: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        with _BOOK_LOCK:
            book = self._load_unlocked(default_state)
            mutator(book)
            self._commit_unlocked(book)
            return deepcopy(book)

    def replace(self, default_state: dict[str, Any], book: dict[str, Any]) -> dict[str, Any]:
        with _BOOK_LOCK:
            normalized = self._normalize(book, default_state)
            self._commit_unlocked(normalized)
            return deepcopy(normalized)

    def _load_unlocked(self, default_state: dict[str, Any]) -> dict[str, Any]:
        if self.book_path.exists():
            try:
                raw = json.loads(self.book_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return self._normalize(raw, default_state)
            except (json.JSONDecodeError, OSError):
                pass

        book = {
            "schema_version": SCHEMA_VERSION,
            "revision": 0,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "state": self._read_json_object(self.state_path) or deepcopy(default_state),
            "trades": self._read_trade_records(),
            "snapshots": self._read_json_object(self.snapshot_path) or {},
        }
        self._commit_unlocked(book)
        return self._normalize(book, default_state)

    def _normalize(self, raw: dict[str, Any], default_state: dict[str, Any]) -> dict[str, Any]:
        state = raw.get("state") if isinstance(raw.get("state"), dict) else deepcopy(default_state)
        trades = raw.get("trades") if isinstance(raw.get("trades"), list) else []
        snapshots = raw.get("snapshots") if isinstance(raw.get("snapshots"), dict) else {}
        return {
            "schema_version": SCHEMA_VERSION,
            "revision": int(raw.get("revision") or 0),
            "updated_at": str(raw.get("updated_at") or datetime.now().isoformat(timespec="seconds")),
            "state": deepcopy(state),
            "trades": [deepcopy(record) for record in trades if isinstance(record, dict)],
            "snapshots": deepcopy(snapshots),
        }

    def _commit_unlocked(self, book: dict[str, Any]) -> None:
        book["schema_version"] = SCHEMA_VERSION
        book["revision"] = int(book.get("revision") or 0) + 1
        book["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._atomic_write_json(self.book_path, book)
        self._atomic_write_json(self.state_path, book.get("state") or {})
        self._atomic_write_text(
            self.trade_path,
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in book.get("trades") or []),
        )
        self._atomic_write_json(self.snapshot_path, book.get("snapshots") or {})

    def _read_trade_records(self) -> list[dict[str, Any]]:
        if not self.trade_path.exists():
            return []
        try:
            lines = self.trade_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return value if isinstance(value, dict) else None

    @classmethod
    def _atomic_write_json(cls, path: Path, value: object) -> None:
        cls._atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
