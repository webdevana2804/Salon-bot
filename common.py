"""Общие помощники для клиентской и админской частей бота."""
from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup

import catalog

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
h = html.escape  # экранирование текста перед вставкой в HTML-сообщение


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def fmt_booking(row) -> str:
    start = datetime.fromisoformat(row["start_at"])
    return (f"<b>{h(catalog.service_name(row['service']))}</b>\n"
            f"🗓 {WEEKDAYS[start.weekday()]} {start:%d.%m.%Y}, {start:%H:%M}\n"
            f"👩 Мастер: {h(catalog.master_name(row['master']))}")


def fmt_days_off() -> str:
    off = sorted(catalog.days_off())
    return ", ".join(WEEKDAYS[d] for d in off) if off else "нет"


def fmt_hours() -> str:
    start, end = catalog.work_hours()
    return f"{start:02d}:00–{end:02d}:00"


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in catalog.admin_ids():
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:  # админ мог не запускать бота
            logging.warning("Не удалось уведомить админа %s: %s", admin_id, e)


CLIENT_COMMANDS = [BotCommand(command="start", description="Главное меню"),
                   BotCommand(command="book", description="Записаться"),
                   BotCommand(command="my", description="Мои записи")]
ADMIN_COMMANDS = CLIENT_COMMANDS + [BotCommand(command="admin", description="Записи на неделю"),
                                    BotCommand(command="settings", description="Настройки салона")]


async def set_admin_menu(bot: Bot, user_id: int, is_admin: bool = True) -> None:
    """Показывает (или убирает) админские команды в меню Telegram у конкретного человека."""
    try:
        if is_admin:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=user_id))
        else:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
    except Exception as e:  # человек ещё не открывал бота
        logging.warning("Не удалось обновить меню команд для %s: %s", user_id, e)
