"""Справочники салона: настройки, услуги и мастера.

Хранятся в SQLite и редактируются администратором через /settings.
Для быстрого доступа держим копию в памяти и перечитываем её после каждого изменения.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import aiosqlite

import config
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS services (
    id       TEXT PRIMARY KEY,
    name     TEXT    NOT NULL,
    duration INTEGER NOT NULL,  -- минуты
    price    INTEGER NOT NULL,  -- рубли
    active   INTEGER NOT NULL DEFAULT 1  -- 0 = удалена (старые записи продолжают её показывать)
);
CREATE TABLE IF NOT EXISTS masters (
    id     TEXT PRIMARY KEY,
    name   TEXT    NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS master_services (
    master_id  TEXT NOT NULL,
    service_id TEXT NOT NULL,
    PRIMARY KEY (master_id, service_id)
);
"""


@dataclass
class Service:
    id: str
    name: str
    duration: int
    price: int
    active: bool


@dataclass
class Master:
    id: str
    name: str
    active: bool
    services: set[str] = field(default_factory=set)


DEFAULT_SETTINGS = {
    "salon_name": config.SALON_NAME,
    "address": config.SALON_ADDRESS,
    "phone": config.SALON_PHONE,
    "greeting": (f"Здравствуйте! 👋 Я бот <b>{config.SALON_NAME}</b>.\n"
                 "Помогу записаться на процедуру, посмотреть или отменить запись."),
    "work_start": str(config.WORK_START),
    "work_end": str(config.WORK_END),
    "days_off": ",".join(str(d) for d in sorted(config.DAYS_OFF)),
}

settings: dict[str, str] = {}
services: dict[str, Service] = {}  # включая удалённые — нужны для старых записей
masters: dict[str, Master] = {}


# ---------- загрузка ----------

async def init() -> None:
    """Создаёт таблицы и при первом запуске переносит наполнение из config.py."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.executemany("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                             DEFAULT_SETTINGS.items())
        cur = await db.execute("SELECT (SELECT COUNT(*) FROM services) + (SELECT COUNT(*) FROM masters)")
        if (await cur.fetchone())[0] == 0:
            await db.executemany(
                "INSERT INTO services (id, name, duration, price) VALUES (?, ?, ?, ?)",
                [(sid, n, d, p) for sid, (n, d, p) in config.SERVICES.items()],
            )
            await db.executemany("INSERT INTO masters (id, name) VALUES (?, ?)",
                                 [(mid, n) for mid, (n, _) in config.MASTERS.items()])
            await db.executemany(
                "INSERT INTO master_services (master_id, service_id) VALUES (?, ?)",
                [(mid, sid) for mid, (_, skills) in config.MASTERS.items() for sid in skills],
            )
        await db.commit()
    await reload()


async def reload() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        settings.clear()
        settings.update({k: v for k, v in await db.execute_fetchall("SELECT key, value FROM settings")})
        services.clear()
        for sid, name, dur, price, active in await db.execute_fetchall(
                "SELECT id, name, duration, price, active FROM services ORDER BY rowid"):
            services[sid] = Service(sid, name, dur, price, bool(active))
        masters.clear()
        for mid, name, active in await db.execute_fetchall(
                "SELECT id, name, active FROM masters ORDER BY rowid"):
            masters[mid] = Master(mid, name, bool(active))
        for mid, sid in await db.execute_fetchall("SELECT master_id, service_id FROM master_services"):
            if mid in masters:
                masters[mid].services.add(sid)


async def _write(sql: str, params: tuple = ()) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, params)
        await db.commit()


# ---------- чтение ----------

def work_hours() -> tuple[int, int]:
    return int(settings["work_start"]), int(settings["work_end"])


def days_off() -> set[int]:
    return {int(d) for d in settings["days_off"].split(",") if d}


def active_masters() -> list[Master]:
    return [m for m in masters.values() if m.active]


def masters_for(service_id: str) -> list[Master]:
    return [m for m in active_masters() if service_id in m.services]


def bookable_services() -> list[Service]:
    """Услуги, которые видит клиент: не удалены и есть хотя бы один мастер."""
    return [s for s in services.values() if s.active and masters_for(s.id)]


def service_name(sid: str) -> str:
    return services[sid].name if sid in services else "Услуга удалена"


def master_name(mid: str) -> str:
    return masters[mid].name if mid in masters else "Мастер удалён"


# ---------- изменение ----------

async def set_setting(key: str, value: str) -> None:
    await _write("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    await reload()


async def add_service(name: str, duration: int, price: int) -> str:
    sid = "s" + uuid.uuid4().hex[:8]
    await _write("INSERT INTO services (id, name, duration, price) VALUES (?, ?, ?, ?)",
                 (sid, name, duration, price))
    await reload()
    return sid


async def update_service(sid: str, column: str, value) -> None:
    if column not in {"name", "duration", "price"}:
        raise ValueError(column)
    await _write(f"UPDATE services SET {column} = ? WHERE id = ?", (value, sid))
    await reload()


async def delete_service(sid: str) -> None:
    await _write("UPDATE services SET active = 0 WHERE id = ?", (sid,))
    await _write("DELETE FROM master_services WHERE service_id = ?", (sid,))
    await reload()


async def add_master(name: str) -> str:
    mid = "m" + uuid.uuid4().hex[:8]
    await _write("INSERT INTO masters (id, name) VALUES (?, ?)", (mid, name))
    await reload()
    return mid


async def rename_master(mid: str, name: str) -> None:
    await _write("UPDATE masters SET name = ? WHERE id = ?", (name, mid))
    await reload()


async def delete_master(mid: str) -> None:
    await _write("UPDATE masters SET active = 0 WHERE id = ?", (mid,))
    await _write("DELETE FROM master_services WHERE master_id = ?", (mid,))
    await reload()


async def toggle_link(mid: str, sid: str) -> None:
    """Включает/выключает услугу у мастера."""
    if sid in masters[mid].services:
        await _write("DELETE FROM master_services WHERE master_id = ? AND service_id = ?", (mid, sid))
    else:
        await _write("INSERT OR IGNORE INTO master_services (master_id, service_id) VALUES (?, ?)",
                     (mid, sid))
    await reload()
