"""File-backed account snapshots and command receipts for the local simulator.

The snapshot and receipt commit in one SQLite transaction. Transitions must be
pure with respect to external systems: a rollback cannot undo external effects.
Multiple instances may share the same database file on a local filesystem.
"""

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StoreError(Exception):
    """The account store could not complete the operation."""


class StoreConflict(StoreError):
    """An account, command identifier, or version conflicts with stored data."""


class AccountNotFound(StoreError):
    """The account has not been created."""


JsonObject = dict[str, Any]
Transition = Callable[[JsonObject], tuple[JsonObject, JsonObject]]
Command = tuple[str, str, int, JsonObject, Transition]
Receipt = JsonObject | Exception
MAX_BATCH = 256


def _encode(value: JsonObject) -> str:
    if not isinstance(value, dict):
        raise StoreError("snapshot, command and result must be JSON objects")

    def validate_keys(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise StoreError("JSON object keys must be strings")
            for child in item.values():
                validate_keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate_keys(child)

    try:
        validate_keys(value)
        return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise StoreError("snapshot, command and result must be valid JSON") from exc


def _decode(value: str) -> JsonObject:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise StoreError("stored JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise StoreError("stored JSON must be an object")
    return decoded


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise StoreError(f"{label} must be a nonempty string of up to 128 characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StoreError(f"{label} must be valid Unicode") from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(path: str | os.PathLike[str]) -> Path:
    raw_path = os.fspath(path)
    if not isinstance(raw_path, str) or raw_path in ("", ":memory:"):
        raise StoreError("provide a filesystem path for a persistent SQLite database")
    return Path(raw_path).absolute()


@contextmanager
def _transaction(path: Path, *, write: bool, initialize: bool = False,
                 enable_wal: bool = False) -> Iterator[sqlite3.Connection]:
    db = None
    try:
        # Ordinary operations must not silently replace a missing database.
        mode = "rwc" if initialize else "rw"
        db = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA synchronous = FULL")
        if enable_wal:
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            if mode != "wal" and db.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                raise StoreError("SQLite could not enable write-ahead logging")
        # Serialize writers before they read balances or look up receipts.
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    except sqlite3.Error as exc:
        raise StoreError("SQLite account store operation failed") from exc
    finally:
        # Closing an active transaction discards every uncommitted write.
        if db is not None:
            db.close()


class Store:
    """SQLite command store using one connection per operation.

    Version 0 is the original snapshot. Each successful new command increments
    the version by one. Callers should retain each command ID, payload, and
    expected version together so retries recover the original result.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = _path(path)
        if self.path.with_name(self.path.name + ".shards").exists():
            raise StoreConflict("a shard directory already exists at this path; open with the stored shard count")
        with _transaction(self.path, write=True, initialize=True, enable_wal=True) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS uta_accounts (
                    account_id TEXT PRIMARY KEY NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 0),
                    snapshot TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS uta_events (
                    account_id TEXT NOT NULL REFERENCES uta_accounts(account_id),
                    version INTEGER NOT NULL CHECK (version >= 0),
                    command_id TEXT,
                    expected_version INTEGER,
                    command TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, version),
                    UNIQUE (account_id, command_id)
                )
            """)

    def _connection(self, *, write: bool = False):
        return _transaction(self.path, write=write)

    @staticmethod
    def _account(db: sqlite3.Connection, account_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT version, snapshot FROM uta_accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise AccountNotFound(f"account {account_id!r} does not exist")
        return row

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> JsonObject:
        state = _decode(row["snapshot"])
        if type(state.get("version")) is not int or state["version"] != row["version"]:
            raise StoreError("stored snapshot and account versions differ")
        return state

    def create(self, account_id: str, state: JsonObject) -> JsonObject:
        """Persist a new account, emitting the initial version-0 audit event."""
        _identifier(account_id, "account ID")
        snapshot = _decode(_encode(state))
        snapshot["version"] = 0
        with self._connection(write=True) as db:
            exists = db.execute(
                "SELECT 1 FROM uta_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            if exists:
                raise StoreConflict(f"account {account_id!r} already exists")
            db.execute(
                "INSERT INTO uta_accounts (account_id, version, snapshot) VALUES (?, 0, ?)",
                (account_id, _encode(snapshot)),
            )
            db.execute(
                """INSERT INTO uta_events
                   (account_id, version, command_id, expected_version, command, result, created_at)
                   VALUES (?, 0, NULL, NULL, ?, ?, ?)""",
                (account_id, _encode({"type": "create", "state": snapshot}),
                 _encode({"version": 0}), _now()),
            )
        return snapshot

    def read(self, account_id: str) -> JsonObject:
        """Return the committed account snapshot and its version."""
        _identifier(account_id, "account ID")
        with self._connection() as db:
            return self._snapshot(self._account(db, account_id))

    def apply(
        self,
        account_id: str,
        command_id: str,
        expected_version: int,
        command: JsonObject,
        transition: Transition,
    ) -> JsonObject:
        """Apply a command to the expected account version or return its receipt.

        The callback gets an independent snapshot and returns a new state and a
        result, both JSON objects. Its financial inputs and outputs should use
        decimal strings. Changed commands cannot reuse a committed command ID.
        """
        with self._connection(write=True) as db:
            return self._apply(db, account_id, command_id, expected_version, command, transition)

    def _apply(self, db: sqlite3.Connection, account_id: str, command_id: str,
               expected_version: int, command: JsonObject, transition: Transition) -> JsonObject:
        _identifier(account_id, "account ID")
        _identifier(command_id, "command ID")
        if type(expected_version) is not int or not 0 <= expected_version < 2**63:
            raise StoreError("expected_version must be a nonnegative 64-bit integer")
        command_json = _encode(command)

        row = self._account(db, account_id)
        previous = db.execute(
            """SELECT expected_version, command, result FROM uta_events
               WHERE account_id = ? AND command_id = ?""",
            (account_id, command_id),
        ).fetchone()
        if previous is not None:
            if previous["expected_version"] != expected_version or previous["command"] != command_json:
                raise StoreConflict("command ID was already used with a different request")
            return _decode(previous["result"])
        if row["version"] != expected_version:
            raise StoreConflict(f"expected version {expected_version}, current version {row['version']}")
        if expected_version == 2**63 - 1:
            raise StoreError("account version limit reached")

        state, result = transition(self._snapshot(row))
        new_state = _decode(_encode(state))
        receipt = _decode(_encode(result))
        version = expected_version + 1
        new_state["version"] = receipt["version"] = version
        db.execute(
            "UPDATE uta_accounts SET version = ?, snapshot = ? WHERE account_id = ?",
            (version, _encode(new_state), account_id),
        )
        db.execute(
            """INSERT INTO uta_events
               (account_id, version, command_id, expected_version, command, result, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (account_id, version, command_id, expected_version, command_json, _encode(receipt), _now()),
        )
        return receipt

    def apply_many(self, items: Sequence[Command]) -> list[Receipt]:
        """Commit up to MAX_BATCH commands once, in input order.

        Known command rejections occupy their original result slots and are
        rolled back independently. The return occurs after a successful SQLite
        commit. A database error aborts the group and returns no receipts.
        """
        if len(items) > MAX_BATCH:
            raise StoreError(f"at most {MAX_BATCH} commands may be committed in one batch")
        if not items:
            return []
        results: list[Receipt] = []
        with self._connection(write=True) as db:
            for item in items:
                db.execute("SAVEPOINT command_item")
                try:
                    receipt: Receipt = self._apply(db, *item)
                except (StoreError, ValueError) as exc:
                    db.execute("ROLLBACK TO SAVEPOINT command_item")
                    receipt = exc
                db.execute("RELEASE SAVEPOINT command_item")
                results.append(receipt)
        return results

    @staticmethod
    def _events(db: sqlite3.Connection, account_id: str) -> list[JsonObject]:
        rows = db.execute(
            """SELECT version, command_id, expected_version, command, result, created_at
               FROM uta_events WHERE account_id = ? ORDER BY version""",
            (account_id,),
        )
        return [dict(row, command=_decode(row["command"]), result=_decode(row["result"])) for row in rows]

    def events(self, account_id: str) -> list[JsonObject]:
        """Return the committed audit entries in per-account version order."""
        _identifier(account_id, "account ID")
        with self._connection() as db:
            self._account(db, account_id)
            return self._events(db, account_id)

    def read_for_replay(self, account_id: str) -> tuple[JsonObject, list[JsonObject]]:
        """Read the account snapshot and its audit history in one read transaction."""
        _identifier(account_id, "account ID")
        with self._connection() as db:
            snapshot = self._snapshot(self._account(db, account_id))
            return snapshot, self._events(db, account_id)


class ShardedStore:
    """Fixed account partitions with independent SQLite WAL writers.

    One local SQLite file owns each account's snapshot and receipts. Sharding
    can spread independent accounts across writers. Commands for one account
    still serialize, and there is no cross-account transaction or repartitioning.
    """

    _ROUTING = "sha256-mod-v1"

    def __init__(self, path: str | os.PathLike[str], *, shards: int = 8):
        if type(shards) is not int or not 1 <= shards <= 64:
            raise StoreError("shard count must be an integer from 1 to 64")
        base = _path(path)
        if base.exists():
            raise StoreConflict("a single database already exists at this path; use a new path for shards")
        self.directory = base.with_name(base.name + ".shards")
        self.shards = shards
        try:
            self.directory.mkdir(exist_ok=True)
        except OSError as exc:
            raise StoreError("could not create the shard directory") from exc

        paths = tuple(self.directory / f"shard-{index:02d}.sqlite3" for index in range(shards))
        manifest = self.directory / "manifest.sqlite3"
        # Commit the routing before creating shard files. A process crash may
        # leave an incomplete set of empty shards, but cannot change routing.
        with _transaction(manifest, write=True, initialize=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS layout (
                              id INTEGER PRIMARY KEY CHECK (id = 1),
                              shard_count INTEGER NOT NULL,
                              routing TEXT NOT NULL,
                              ready INTEGER NOT NULL CHECK (ready IN (0, 1))
                          )""")
            layout = db.execute("SELECT shard_count, routing FROM layout WHERE id = 1").fetchone()
            if layout is None:
                if any(self.directory.glob("shard-*.sqlite3*")):
                    raise StoreError("shard files exist without a valid routing manifest")
                db.execute("INSERT INTO layout VALUES (1, ?, ?, 0)", (shards, self._ROUTING))
            elif tuple(layout) != (shards, self._ROUTING):
                raise StoreConflict("requested shard count or routing differs from the stored layout")

        # Keep concurrent initializers from attempting to change the same
        # SQLite journal mode and validate that ready shards still exist.
        with _transaction(manifest, write=True) as db:
            ready = db.execute("SELECT ready FROM layout WHERE id = 1").fetchone()[0]
            if ready and any(not path.is_file() for path in paths):
                raise StoreError("one or more configured shard files are missing")
            self._stores = tuple(Store(path) for path in paths)
            if not ready:
                db.execute("UPDATE layout SET ready = 1 WHERE id = 1")

    def _for_account(self, account_id: str) -> Store:
        _identifier(account_id, "account ID")
        digest = hashlib.sha256(account_id.encode("utf-8")).digest()
        return self._stores[int.from_bytes(digest, "big") % self.shards]

    def create(self, account_id: str, state: JsonObject) -> JsonObject:
        return self._for_account(account_id).create(account_id, state)

    def read(self, account_id: str) -> JsonObject:
        return self._for_account(account_id).read(account_id)

    def apply(self, account_id: str, command_id: str, expected_version: int,
              command: JsonObject, transition: Transition) -> JsonObject:
        return self._for_account(account_id).apply(account_id, command_id, expected_version, command, transition)

    def events(self, account_id: str) -> list[JsonObject]:
        return self._for_account(account_id).events(account_id)

    def read_for_replay(self, account_id: str) -> tuple[JsonObject, list[JsonObject]]:
        return self._for_account(account_id).read_for_replay(account_id)

    def apply_many(self, items: Sequence[Command]) -> list[Receipt]:
        """Commit per-shard command groups concurrently without cross-shard atomicity."""
        if len(items) > MAX_BATCH:
            raise StoreError(f"at most {MAX_BATCH} commands may be committed in one batch")
        groups: dict[Store, list[tuple[int, Command]]] = {}
        results: dict[int, Receipt] = {}
        for index, item in enumerate(items):
            try:
                store = self._for_account(item[0])
            except StoreError as exc:
                results[index] = exc
            else:
                groups.setdefault(store, []).append((index, item))

        def commit_group(store: Store, group: list[tuple[int, Command]]) -> dict[int, Receipt]:
            indices, commands = zip(*group)
            try:
                return dict(zip(indices, store.apply_many(commands)))
            except StoreError as exc:
                return dict.fromkeys(indices, exc)

        if len(groups) == 1:
            store, group = next(iter(groups.items()))
            results.update(commit_group(store, group))
        elif groups:
            with ThreadPoolExecutor(max_workers=min(8, len(groups))) as pool:
                futures = [pool.submit(commit_group, store, group) for store, group in groups.items()]
                for future in futures:
                    results.update(future.result())
        return [results[index] for index in range(len(items))]
