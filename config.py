"""Настройки салона. Правьте услуги, мастеров и график здесь."""
from __future__ import annotations

import os
from pathlib import Path


def _load_env(path: Path = Path(__file__).with_name(".env")) -> None:
    """Простейший загрузчик .env, чтобы не тянуть лишние зависимости."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_env()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).with_name("salon.db")))

SALON_NAME = "Салон красоты «Бархат»"
SALON_ADDRESS = "г. Москва, ул. Примерная, д. 1"
SALON_PHONE = "+7 (900) 000-00-00"

# id -> (название, длительность в минутах, цена в рублях)
SERVICES = {
    "haircut": ("Стрижка женская", 60, 2000),
    "coloring": ("Окрашивание", 120, 5000),
    "manicure": ("Маникюр с покрытием", 90, 2500),
    "pedicure": ("Педикюр", 90, 3000),
    "brows": ("Коррекция и окрашивание бровей", 30, 1200),
}

# id -> (имя, список id услуг, которые выполняет мастер)
MASTERS = {
    "anna": ("Анна", ["haircut", "coloring"]),
    "olga": ("Ольга", ["manicure", "pedicure"]),
    "maria": ("Мария", ["brows", "manicure"]),
}

WORK_START = 10  # час открытия
WORK_END = 20  # час закрытия
SLOT_STEP_MIN = 30  # шаг сетки записи
DAYS_AHEAD = 7  # на сколько дней вперёд можно записаться
DAYS_OFF = {6}  # выходные: 0 = пн ... 6 = вс
