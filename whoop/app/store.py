"""This app's own SQLite database — journal, workouts, habit definitions.

Deliberately a *separate file* from NOOP's. NOOP's database is opened read-only
and never written to; everything you type lives here, so a NOOP update, reinstall
or schema migration cannot touch it. The two are joined only by the day key.

Migrations run on open and are keyed off `PRAGMA user_version`, so an older
database is upgraded in place rather than being silently incompatible.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

HABIT_TYPES = ("bool", "scale", "number")

DEFAULT_HABITS: tuple[dict[str, Any], ...] = (
    {"key": "caffeine", "label": "Caffeine", "type": "bool"},
    {"key": "alcohol", "label": "Alcohol", "type": "bool"},
    {"key": "late_meal", "label": "Late meal", "type": "bool"},
    {"key": "screen_time", "label": "Screen time before bed", "type": "scale",
     "min_value": 1.0, "max_value": 5.0},
    {"key": "meditation", "label": "Meditation", "type": "bool"},
)
"""Seeded once on first run, purely so the journal is not an empty screen.

These are the examples from the project brief. Every one can be renamed, retyped,
reordered or archived, and new ones added — nothing here is privileged.
"""

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE habits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT    NOT NULL UNIQUE,
            label       TEXT    NOT NULL,
            type        TEXT    NOT NULL CHECK (type IN ('bool','scale','number')),
            unit        TEXT,
            min_value   REAL,
            max_value   REAL,
            position    INTEGER NOT NULL DEFAULT 0,
            archived    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL
        )
        """,
        """
        CREATE TABLE habit_entries (
            day        TEXT    NOT NULL,
            habit_id   INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
            value      REAL    NOT NULL,
            updated_at TEXT    NOT NULL,
            PRIMARY KEY (day, habit_id)
        )
        """,
        "CREATE INDEX idx_habit_entries_day ON habit_entries(day)",
        """
        CREATE TABLE day_notes (
            day        TEXT PRIMARY KEY,
            note       TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE workouts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            day            TEXT    NOT NULL,
            start_ts       INTEGER,
            end_ts         INTEGER,
            type           TEXT    NOT NULL,
            duration_s     INTEGER NOT NULL,
            exertion       INTEGER,
            notes          TEXT,
            source         TEXT    NOT NULL DEFAULT 'manual'
                           CHECK (source IN ('manual','confirmed')),
            suggestion_key TEXT UNIQUE,
            avg_hr         REAL,
            peak_hr        INTEGER,
            created_at     TEXT    NOT NULL,
            updated_at     TEXT    NOT NULL
        )
        """,
        "CREATE INDEX idx_workouts_day ON workouts(day)",
        "CREATE INDEX idx_workouts_start ON workouts(start_ts)",
        """
        CREATE TABLE app_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE dismissed_suggestions (
            suggestion_key TEXT PRIMARY KEY,
            day            TEXT NOT NULL,
            dismissed_at   TEXT NOT NULL
        )
        """,
    ),
}


class StoreError(RuntimeError):
    """A caller-fixable problem: bad habit type, unknown id, invalid value."""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Habit:
    id: int
    key: str
    label: str
    type: str
    unit: str | None
    min_value: float | None
    max_value: float | None
    position: int
    archived: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Habit:
        return cls(
            id=row["id"], key=row["key"], label=row["label"], type=row["type"],
            unit=row["unit"], min_value=row["min_value"], max_value=row["max_value"],
            position=row["position"], archived=bool(row["archived"]),
        )

    def bounds(self) -> tuple[float, float] | None:
        """Effective numeric bounds for this habit's type."""
        if self.type == "bool":
            return (0.0, 1.0)
        if self.type == "scale":
            return (self.min_value if self.min_value is not None else 1.0,
                    self.max_value if self.max_value is not None else 5.0)
        if self.min_value is None and self.max_value is None:
            return None
        return (self.min_value if self.min_value is not None else float("-inf"),
                self.max_value if self.max_value is not None else float("inf"))

    def as_dict(self) -> dict[str, Any]:
        bounds = self.bounds()
        return {
            "id": self.id, "key": self.key, "label": self.label, "type": self.type,
            "unit": self.unit,
            "min_value": bounds[0] if bounds and bounds[0] != float("-inf") else None,
            "max_value": bounds[1] if bounds and bounds[1] != float("inf") else None,
            "position": self.position, "archived": self.archived,
        }

    def validate(self, value: float) -> float:
        """Coerce and range-check a value, or raise StoreError."""
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise StoreError(f"'{self.label}' needs a number, got {value!r}") from exc
        if num != num or num in (float("inf"), float("-inf")):
            raise StoreError(f"'{self.label}' got a non-finite value")

        if self.type == "bool":
            if num not in (0.0, 1.0):
                raise StoreError(f"'{self.label}' is a yes/no habit; value must be 0 or 1")
            return num
        if self.type == "scale":
            lo, hi = self.bounds()  # type: ignore[misc]
            if num != int(num):
                raise StoreError(f"'{self.label}' is a {int(lo)}-{int(hi)} scale; whole numbers only")
            if not (lo <= num <= hi):
                raise StoreError(f"'{self.label}' must be between {int(lo)} and {int(hi)}")
            return float(int(num))
        bounds = self.bounds()
        if bounds and not (bounds[0] <= num <= bounds[1]):
            raise StoreError(f"'{self.label}' must be between {bounds[0]} and {bounds[1]}")
        return num


class AppStore:
    """Read-write access to this app's own database."""

    def __init__(self, path: Path):
        self.path = path
        self._migrated = False

    # -- connection ------------------------------------------------------

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 4000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> int:
        """Apply pending migrations. Idempotent; safe to call on every start."""
        with self.connect() as conn:
            # WAL survives a hard kill better than the rollback journal, and this
            # is our file so changing its journal mode is ours to do.
            conn.execute("PRAGMA journal_mode = WAL")
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            for version in sorted(MIGRATIONS):
                if version > current:
                    for statement in MIGRATIONS[version]:
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {version}")
                    current = version
        self._migrated = True
        return current

    def ensure_ready(self) -> None:
        if not self._migrated:
            self.migrate()
            self.seed_default_habits()

    def version(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    # -- habits ----------------------------------------------------------

    def seed_default_habits(self) -> int:
        """Insert the starter habits, once, into a genuinely new database.

        Guarded by a persisted marker rather than by "is the habits table empty".
        Deleting every habit on purpose is a choice, and it has to survive a
        restart — otherwise the app would keep handing back habits you removed.
        """
        with self.connect() as conn:
            seeded = conn.execute(
                "SELECT value FROM app_meta WHERE key = 'habits_seeded'"
            ).fetchone()
            if seeded is not None:
                return 0
            conn.execute("INSERT INTO app_meta (key, value) VALUES ('habits_seeded', ?)",
                         (_now(),))
            if conn.execute("SELECT COUNT(*) AS n FROM habits").fetchone()["n"]:
                return 0
            for position, spec in enumerate(DEFAULT_HABITS):
                conn.execute(
                    "INSERT INTO habits (key, label, type, unit, min_value, max_value, "
                    "position, archived, created_at) VALUES (?,?,?,?,?,?,?,0,?)",
                    (spec["key"], spec["label"], spec["type"], spec.get("unit"),
                     spec.get("min_value"), spec.get("max_value"), position, _now()),
                )
            return len(DEFAULT_HABITS)

    def habits(self, include_archived: bool = False) -> list[Habit]:
        sql = "SELECT * FROM habits"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY position ASC, id ASC"
        with self.connect() as conn:
            return [Habit.from_row(r) for r in conn.execute(sql)]

    def habit(self, habit_id: int) -> Habit:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        if row is None:
            raise StoreError(f"No habit with id {habit_id}")
        return Habit.from_row(row)

    def create_habit(self, key: str, label: str, type_: str, unit: str | None = None,
                     min_value: float | None = None, max_value: float | None = None,
                     position: int | None = None) -> Habit:
        key = (key or "").strip()
        label = (label or "").strip()
        if not key or not label:
            raise StoreError("A habit needs both a key and a label")
        if type_ not in HABIT_TYPES:
            raise StoreError(f"Habit type must be one of {', '.join(HABIT_TYPES)}")
        if min_value is not None and max_value is not None and min_value >= max_value:
            raise StoreError("min_value must be below max_value")

        with self.connect() as conn:
            if position is None:
                row = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM habits").fetchone()
                position = int(row["p"])
            try:
                cur = conn.execute(
                    "INSERT INTO habits (key, label, type, unit, min_value, max_value, "
                    "position, archived, created_at) VALUES (?,?,?,?,?,?,?,0,?)",
                    (key, label, type_, unit, min_value, max_value, position, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"A habit with key '{key}' already exists") from exc
            habit_id = int(cur.lastrowid)
            row = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        return Habit.from_row(row)

    def update_habit(self, habit_id: int, **fields: Any) -> Habit:
        allowed = {"label", "type", "unit", "min_value", "max_value", "position", "archived"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "type" in updates and updates["type"] not in HABIT_TYPES:
            raise StoreError(f"Habit type must be one of {', '.join(HABIT_TYPES)}")
        if "archived" in updates:
            updates["archived"] = int(bool(updates["archived"]))
        if not updates:
            return self.habit(habit_id)

        assignments = ", ".join(f'"{k}" = ?' for k in updates)
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE habits SET {assignments} WHERE id = ?",
                (*updates.values(), habit_id),
            )
            if cur.rowcount == 0:
                raise StoreError(f"No habit with id {habit_id}")
            row = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        return Habit.from_row(row)

    def delete_habit(self, habit_id: int) -> int:
        """Hard delete, cascading to its entries. Prefer archiving."""
        with self.connect() as conn:
            n = conn.execute("DELETE FROM habits WHERE id = ?", (habit_id,)).rowcount
        if n == 0:
            raise StoreError(f"No habit with id {habit_id}")
        return n

    # -- journal ---------------------------------------------------------

    def journal_day(self, day: str) -> dict[str, Any]:
        """One day: every active habit with its value (or None) plus the note."""
        habits = self.habits()
        with self.connect() as conn:
            entries = {
                r["habit_id"]: r["value"]
                for r in conn.execute("SELECT habit_id, value FROM habit_entries WHERE day = ?", (day,))
            }
            note_row = conn.execute("SELECT note FROM day_notes WHERE day = ?", (day,)).fetchone()

        return {
            "day": day,
            "habits": [{**h.as_dict(), "value": entries.get(h.id)} for h in habits],
            "note": note_row["note"] if note_row else "",
            "entry_count": sum(1 for h in habits if entries.get(h.id) is not None),
        }

    def set_habit_value(self, day: str, habit_id: int, value: float | None) -> None:
        """Set or clear one habit's value for a day. Clearing is not zero."""
        habit = self.habit(habit_id)
        with self.connect() as conn:
            if value is None:
                conn.execute("DELETE FROM habit_entries WHERE day = ? AND habit_id = ?",
                             (day, habit_id))
                return
            checked = habit.validate(value)
            conn.execute(
                "INSERT INTO habit_entries (day, habit_id, value, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(day, habit_id) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (day, habit_id, checked, _now()),
            )

    def set_note(self, day: str, note: str) -> None:
        note = note or ""
        with self.connect() as conn:
            if not note.strip():
                conn.execute("DELETE FROM day_notes WHERE day = ?", (day,))
                return
            conn.execute(
                "INSERT INTO day_notes (day, note, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET note = excluded.note, "
                "updated_at = excluded.updated_at",
                (day, note, _now()),
            )

    def journal_range(self, start_day: str, end_day: str) -> dict[str, dict[str, Any]]:
        """Journal values keyed by day, then by habit key. For export/correlations."""
        habits = {h.id: h for h in self.habits(include_archived=True)}
        out: dict[str, dict[str, Any]] = {}
        with self.connect() as conn:
            for row in conn.execute(
                "SELECT day, habit_id, value FROM habit_entries WHERE day BETWEEN ? AND ? "
                "ORDER BY day ASC", (start_day, end_day),
            ):
                habit = habits.get(row["habit_id"])
                if habit is None:
                    continue
                out.setdefault(row["day"], {"values": {}, "note": ""})
                out[row["day"]]["values"][habit.key] = row["value"]
            for row in conn.execute(
                "SELECT day, note FROM day_notes WHERE day BETWEEN ? AND ?", (start_day, end_day),
            ):
                out.setdefault(row["day"], {"values": {}, "note": ""})
                out[row["day"]]["note"] = row["note"]
        return out

    def journalled_days(self, start_day: str, end_day: str) -> set[str]:
        """Days with any journal content — drives the 'already logged' markers."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT day FROM habit_entries WHERE day BETWEEN ? AND ? "
                "UNION SELECT day FROM day_notes WHERE day BETWEEN ? AND ?",
                (start_day, end_day, start_day, end_day),
            ).fetchall()
        return {r["day"] for r in rows}

    # -- workouts --------------------------------------------------------

    def workouts(self, start_day: str | None = None, end_day: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM workouts"
        params: list[Any] = []
        if start_day and end_day:
            sql += " WHERE day BETWEEN ? AND ?"
            params = [start_day, end_day]
        sql += " ORDER BY COALESCE(start_ts, 0) DESC, id DESC"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def workout(self, workout_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,)).fetchone()
        if row is None:
            raise StoreError(f"No workout with id {workout_id}")
        return dict(row)

    def create_workout(self, day: str, type_: str, duration_s: int,
                       exertion: int | None = None, notes: str | None = None,
                       start_ts: int | None = None, end_ts: int | None = None,
                       source: str = "manual", suggestion_key: str | None = None,
                       avg_hr: float | None = None, peak_hr: int | None = None) -> dict[str, Any]:
        type_ = (type_ or "").strip()
        if not type_:
            raise StoreError("A workout needs a type")
        if duration_s is None or duration_s <= 0:
            raise StoreError("Duration must be greater than zero")
        if exertion is not None and not (1 <= int(exertion) <= 10):
            raise StoreError("Perceived exertion is a 1-10 scale")
        if source not in ("manual", "confirmed"):
            raise StoreError("source must be 'manual' or 'confirmed'")

        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO workouts (day, start_ts, end_ts, type, duration_s, exertion, "
                    "notes, source, suggestion_key, avg_hr, peak_hr, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (day, start_ts, end_ts, type_, int(duration_s),
                     int(exertion) if exertion is not None else None,
                     notes, source, suggestion_key, avg_hr, peak_hr, _now(), _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("That suggestion has already been logged") from exc
            row = conn.execute("SELECT * FROM workouts WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def update_workout(self, workout_id: int, **fields: Any) -> dict[str, Any]:
        allowed = {"day", "type", "duration_s", "exertion", "notes", "start_ts", "end_ts"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "exertion" in updates and not (1 <= int(updates["exertion"]) <= 10):
            raise StoreError("Perceived exertion is a 1-10 scale")
        if "duration_s" in updates and int(updates["duration_s"]) <= 0:
            raise StoreError("Duration must be greater than zero")
        if not updates:
            return self.workout(workout_id)

        updates["updated_at"] = _now()
        assignments = ", ".join(f'"{k}" = ?' for k in updates)
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE workouts SET {assignments} WHERE id = ?",
                (*updates.values(), workout_id),
            )
            if cur.rowcount == 0:
                raise StoreError(f"No workout with id {workout_id}")
            row = conn.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,)).fetchone()
        return dict(row)

    def delete_workout(self, workout_id: int) -> None:
        with self.connect() as conn:
            if conn.execute("DELETE FROM workouts WHERE id = ?", (workout_id,)).rowcount == 0:
                raise StoreError(f"No workout with id {workout_id}")

    # -- suggestions -----------------------------------------------------

    def dismiss_suggestion(self, key: str, day: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO dismissed_suggestions (suggestion_key, day, dismissed_at) "
                "VALUES (?,?,?) ON CONFLICT(suggestion_key) DO NOTHING",
                (key, day, _now()),
            )

    def undismiss_suggestion(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM dismissed_suggestions WHERE suggestion_key = ?", (key,))

    def dismissed_keys(self, start_day: str, end_day: str) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT suggestion_key FROM dismissed_suggestions WHERE day BETWEEN ? AND ?",
                (start_day, end_day),
            ).fetchall()
        return {r["suggestion_key"] for r in rows}

    def confirmed_keys(self, start_day: str, end_day: str) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT suggestion_key FROM workouts "
                "WHERE suggestion_key IS NOT NULL AND day BETWEEN ? AND ?",
                (start_day, end_day),
            ).fetchall()
        return {r["suggestion_key"] for r in rows}

    def logged_spans(self, start_day: str, end_day: str) -> list[tuple[int, int]]:
        """Time spans already covered by a logged workout, for overlap suppression."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT start_ts, end_ts FROM workouts "
                "WHERE start_ts IS NOT NULL AND end_ts IS NOT NULL AND day BETWEEN ? AND ?",
                (start_day, end_day),
            ).fetchall()
        return [(int(r["start_ts"]), int(r["end_ts"])) for r in rows]

    # -- export ----------------------------------------------------------

    def export_all(self) -> dict[str, list[dict[str, Any]]]:
        """Every row this app owns. Phase 5 wires it to CSV/JSON download."""
        tables = ("habits", "habit_entries", "day_notes", "workouts", "dismissed_suggestions")
        out: dict[str, list[dict[str, Any]]] = {}
        with self.connect() as conn:
            for table in tables:
                out[table] = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
        return out

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            def count(table: str) -> int:
                return int(conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()["n"])
            return {
                "path": str(self.path),
                "schema_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
                "habits": count("habits"),
                "habit_entries": count("habit_entries"),
                "day_notes": count("day_notes"),
                "workouts": count("workouts"),
                "dismissed_suggestions": count("dismissed_suggestions"),
            }


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value)}")


__all__ = ["AppStore", "Habit", "StoreError", "DEFAULT_HABITS", "HABIT_TYPES",
           "SCHEMA_VERSION", "json_default"]
