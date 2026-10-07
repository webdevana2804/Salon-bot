"""Хранение записей в SQLite."""
from __future__ import annotations

from datetime import datetime

import aiosqlite

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    client_name TEXT    NOT NULL,
    phone       TEXT    NOT NULL,
    service     TEXT    NOT NULL,
    master      TEXT    NOT NULL,
    start_at    TEXT    NOT NULL,  -- ISO: 2026-09-30T14:00
    end_at      TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'active',  -- active | cancelled
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_master_time ON bookings(master, start_at);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def busy_intervals(master: str, day: str) -> list[tuple[datetime, datetime]]:
    """Занятые интервалы мастера на дату (day = 'YYYY-MM-DD')."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT start_at, end_at FROM bookings "
            "WHERE master = ? AND status = 'active' AND start_at LIKE ?",
            (master, f"{day}%"),
        )
        rows = await cur.fetchall()
    return [(datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in rows]


async def create_booking(
    user_id: int, name: str, phone: str, service: str, master: str,
    start: datetime, end: datetime,
) -> int | None:
    """Создаёт запись. Возвращает id или None, если слот уже заняли."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")  # защита от двойной записи на одно время
        cur = await db.execute(
            "SELECT 1 FROM bookings WHERE master = ? AND status = 'active' "
            "AND start_at < ? AND end_at > ?",
            (master, end.isoformat(timespec="minutes"), start.isoformat(timespec="minutes")),
        )
        if await cur.fetchone():
            await db.rollback()
            return None
        cur = await db.execute(
            "INSERT INTO bookings (user_id, client_name, phone, service, master, start_at, end_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, phone, service, master,
             start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")),
        )
        await db.commit()
        return cur.lastrowid


async def user_bookings(user_id: int) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM bookings WHERE user_id = ? AND status = 'active' "
            "AND start_at >= ? ORDER BY start_at",
            (user_id, datetime.now().isoformat(timespec="minutes")),
        )
        return list(await cur.fetchall())


async def bookings_between(start: datetime, end: datetime) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM bookings WHERE status = 'active' "
            "AND start_at >= ? AND start_at < ? ORDER BY start_at",
            (start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")),
        )
        return list(await cur.fetchall())


async def cancel_booking(booking_id: int, user_id: int | None = None) -> aiosqlite.Row | None:
    """Отменяет запись. Если передан user_id — только свою."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM bookings WHERE id = ? AND status = 'active'"
        params: tuple = (booking_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            params += (user_id,)
        cur = await db.execute(query, params)
        row = await cur.fetchone()
        if row:
            await db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
            await db.commit()
        return row


async def count_future(column: str, value: str) -> int:
    """Сколько предстоящих записей у услуги (column='service') или мастера (column='master')."""
    if column not in {"service", "master"}:
        raise ValueError(column)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"SELECT COUNT(*) FROM bookings WHERE {column} = ? AND status = 'active' AND start_at >= ?",
            (value, datetime.now().isoformat(timespec="minutes")),
        )
        return (await cur.fetchone())[0]
