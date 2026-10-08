"""Telegram-бот для записи на процедуры в салон красоты (aiogram 3)."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup

import admin
import catalog
import db
from common import (
    CLIENT_COMMANDS, WEEKDAYS, fmt_booking, fmt_days_off, fmt_hours, h, inline, notify_admins,
    set_admin_menu,
)
from config import BOT_TOKEN, DAYS_AHEAD, SLOT_STEP_MIN

router = Router()
BTN_BOOK, BTN_MY, BTN_CONTACTS = "📅 Записаться", "📋 Мои записи", "📍 Контакты"


class Booking(StatesGroup):
    service = State()
    master = State()
    day = State()
    time = State()
    name = State()
    phone = State()
    confirm = State()


# ---------- клавиатуры ----------

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_BOOK)],
                  [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_CONTACTS)]],
        resize_keyboard=True,
    )


CANCEL_ROW = [("✖️ Отмена", "abort")]


def services_kb() -> InlineKeyboardMarkup:
    rows = [[(f"{s.name} — {s.price} ₽", f"svc:{s.id}")] for s in catalog.bookable_services()]
    return inline(rows + [CANCEL_ROW])


def masters_kb(service_id: str) -> InlineKeyboardMarkup:
    rows = [[(m.name, f"mst:{m.id}")] for m in catalog.masters_for(service_id)]
    return inline(rows + [[("⬅️ Назад", "back:service")], CANCEL_ROW])


def days_kb() -> InlineKeyboardMarkup:
    today = date.today()
    buttons = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        if d.weekday() in catalog.days_off():
            continue
        buttons.append((f"{WEEKDAYS[d.weekday()]} {d:%d.%m}", f"day:{d.isoformat()}"))
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    return inline(rows + [[("⬅️ Назад", "back:master")], CANCEL_ROW])


def times_kb(slots: list[datetime]) -> InlineKeyboardMarkup:
    buttons = [(f"{s:%H:%M}", f"time:{s:%H:%M}") for s in slots]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    return inline(rows + [[("⬅️ Назад", "back:day")], CANCEL_ROW])


# ---------- логика слотов ----------

async def free_slots(master: str, service: str, day: date) -> list[datetime]:
    duration = timedelta(minutes=catalog.services[service].duration)
    busy = await db.busy_intervals(master, day.isoformat())
    now = datetime.now()
    work_start, work_end = catalog.work_hours()
    t = datetime.combine(day, datetime.min.time()) + timedelta(hours=work_start)
    close = t + timedelta(hours=work_end - work_start)
    slots = []
    while t + duration <= close:
        if t > now and all(not (t < e and t + duration > s) for s, e in busy):
            slots.append(t)
        t += timedelta(minutes=SLOT_STEP_MIN)
    return slots


# ---------- общие команды ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = catalog.settings["greeting"]
    user = message.from_user
    if user.id in catalog.extra_admins and not catalog.extra_admins[user.id]:
        # админа добавили по ID до того, как он открыл бота, — запоминаем имя
        await catalog.add_admin(user.id, user.full_name)
    if catalog.is_admin(user.id):
        text += ("\n\n<i>Вы администратор:\n/admin — записи на неделю\n"
                 "/settings — услуги, мастера, тексты и график</i>")
    await message.answer(text, reply_markup=main_menu())


@router.message(F.text == BTN_CONTACTS)
async def contacts(message: Message):
    s = catalog.settings
    await message.answer(
        f"<b>{h(s['salon_name'])}</b>\n📍 {h(s['address'])}\n📞 {h(s['phone'])}\n"
        f"🕙 Часы работы: {fmt_hours()}\n📅 Выходные: {fmt_days_off()}"
    )


@router.callback_query(F.data == "abort")
async def abort(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Запись отменена. Возвращайтесь, когда будет удобно 🌸")


# ---------- сценарий записи ----------

@router.message(F.text == BTN_BOOK)
@router.message(Command("book"))
async def start_booking(message: Message, state: FSMContext):
    await state.clear()
    if not catalog.bookable_services():
        await message.answer("Сейчас запись недоступна. Позвоните нам: " + h(catalog.settings["phone"]))
        return
    await state.set_state(Booking.service)
    await message.answer("Выберите услугу:", reply_markup=services_kb())


@router.callback_query(Booking.service, F.data.startswith("svc:"))
async def choose_service(call: CallbackQuery, state: FSMContext):
    service = call.data.split(":", 1)[1]
    if not catalog.masters_for(service):
        await call.answer("Эта услуга сейчас недоступна", show_alert=True)
        await call.message.edit_text("Выберите услугу:", reply_markup=services_kb())
        return
    await state.update_data(service=service)
    await state.set_state(Booking.master)
    svc = catalog.services[service]
    await call.message.edit_text(
        f"Услуга: <b>{h(svc.name)}</b> ({svc.duration} мин, {svc.price} ₽)\n\nВыберите мастера:",
        reply_markup=masters_kb(service),
    )


@router.callback_query(Booking.master, F.data.startswith("mst:"))
async def choose_master(call: CallbackQuery, state: FSMContext):
    await state.update_data(master=call.data.split(":", 1)[1])
    await state.set_state(Booking.day)
    await call.message.edit_text("Выберите дату:", reply_markup=days_kb())


@router.callback_query(Booking.day, F.data.startswith("day:"))
async def choose_day(call: CallbackQuery, state: FSMContext):
    day = date.fromisoformat(call.data.split(":", 1)[1])
    data = await state.get_data()
    slots = await free_slots(data["master"], data["service"], day)
    if not slots:
        await call.answer("На этот день свободного времени нет, выберите другой 🙏", show_alert=True)
        return
    await state.update_data(day=day.isoformat())
    await state.set_state(Booking.time)
    await call.message.edit_text(
        f"{WEEKDAYS[day.weekday()]} {day:%d.%m}. Свободное время:", reply_markup=times_kb(slots)
    )


@router.callback_query(F.data.startswith("back:"))
async def go_back(call: CallbackQuery, state: FSMContext):
    target = call.data.split(":", 1)[1]
    data = await state.get_data()
    if target == "service":
        await state.set_state(Booking.service)
        await call.message.edit_text("Выберите услугу:", reply_markup=services_kb())
    elif target == "master" and "service" in data:
        await state.set_state(Booking.master)
        await call.message.edit_text("Выберите мастера:", reply_markup=masters_kb(data["service"]))
    elif target == "day" and "master" in data:
        await state.set_state(Booking.day)
        await call.message.edit_text("Выберите дату:", reply_markup=days_kb())
    else:
        await call.answer("Начните запись заново", show_alert=True)


@router.callback_query(Booking.time, F.data.startswith("time:"))
async def choose_time(call: CallbackQuery, state: FSMContext):
    await state.update_data(time=call.data.split(":", 1)[1])
    await state.set_state(Booking.name)
    await call.message.edit_text("Отлично! Как к вам обращаться? Напишите ваше имя.")


@router.message(Booking.name, F.text)
async def enter_name(message: Message, state: FSMContext):
    name = html.escape(message.text.strip())
    if not 2 <= len(name) <= 50:
        await message.answer("Пожалуйста, введите имя (от 2 до 50 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(Booking.phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await message.answer("Оставьте номер телефона — нажмите кнопку ниже или введите вручную.",
                         reply_markup=kb)


@router.message(Booking.phone)
async def enter_phone(message: Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
    else:
        phone = (message.text or "").strip()
        if not re.fullmatch(r"\+?[\d\s\-()]{10,18}", phone):
            await message.answer("Похоже, номер указан неверно. Пример: +7 900 123-45-67")
            return
    phone = html.escape(phone)
    await state.update_data(phone=phone)
    await state.set_state(Booking.confirm)
    data = await state.get_data()
    svc = catalog.services[data["service"]]
    day = date.fromisoformat(data["day"])
    await message.answer("Почти готово!", reply_markup=main_menu())
    await message.answer(
        "Проверьте данные записи:\n\n"
        f"💅 Услуга: <b>{h(svc.name)}</b> ({svc.duration} мин)\n"
        f"👩 Мастер: {h(catalog.master_name(data['master']))}\n"
        f"🗓 {WEEKDAYS[day.weekday()]} {day:%d.%m.%Y}, {data['time']}\n"
        f"💰 Стоимость: {svc.price} ₽\n"
        f"🙋 {data['name']}, {phone}",
        reply_markup=inline([[("✅ Подтвердить", "confirm")], CANCEL_ROW]),
    )


@router.callback_query(Booking.confirm, F.data == "confirm")
async def confirm(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    start = datetime.fromisoformat(f"{data['day']}T{data['time']}")
    end = start + timedelta(minutes=catalog.services[data["service"]].duration)
    booking_id = await db.create_booking(
        call.from_user.id, data["name"], data["phone"], data["service"], data["master"], start, end,
    )
    if booking_id is None:
        await state.set_state(Booking.day)
        await call.message.edit_text("Увы, это время только что заняли 😔 Выберите другую дату/время:",
                                     reply_markup=days_kb())
        return
    await state.clear()
    await call.message.edit_text(
        f"🎉 Вы записаны! Номер записи: <b>#{booking_id}</b>\n\n"
        f"Ждём вас по адресу: {h(catalog.settings['address'])}\n"
        f"Посмотреть или отменить запись можно в разделе «{BTN_MY}»."
    )
    await notify_admins(bot, (
        f"🆕 Новая запись #{booking_id}\n"
        f"{h(catalog.service_name(data['service']))} — {h(catalog.master_name(data['master']))}\n"
        f"🗓 {start:%d.%m.%Y %H:%M}\n🙋 {data['name']}, {data['phone']}"
    ))


# ---------- мои записи ----------

@router.message(F.text == BTN_MY)
@router.message(Command("my"))
async def my_bookings(message: Message):
    rows = await db.user_bookings(message.from_user.id)
    if not rows:
        await message.answer("У вас пока нет предстоящих записей.")
        return
    for row in rows:
        await message.answer(
            f"Запись #{row['id']}\n{fmt_booking(row)}",
            reply_markup=inline([[("❌ Отменить запись", f"ucancel:{row['id']}")]]),
        )


@router.callback_query(F.data.startswith("ucancel:"))
async def user_cancel(call: CallbackQuery, bot: Bot):
    row = await db.cancel_booking(int(call.data.split(":", 1)[1]), call.from_user.id)
    if not row:
        await call.answer("Запись не найдена или уже отменена", show_alert=True)
        return
    await call.message.edit_text(f"Запись #{row['id']} отменена.\n{fmt_booking(row)}")
    await notify_admins(bot, f"❌ Клиент отменил запись #{row['id']}\n{fmt_booking(row)}\n"
                             f"🙋 {row['client_name']}, {row['phone']}")


@router.callback_query()
async def stale_callback(call: CallbackQuery):
    """Нажатие на кнопку из устаревшего сообщения."""
    await call.answer("Эта кнопка уже неактуальна. Начните заново: /start", show_alert=True)


async def auto_answer_callback(handler, event: CallbackQuery, data):
    """Убирает «часики» на кнопке, если хендлер сам не ответил на callback."""
    result = await handler(event, data)
    try:
        await event.answer()
    except TelegramBadRequest:
        pass  # уже ответили внутри хендлера
    return result


async def set_commands(bot: Bot) -> None:
    """Меню команд в Telegram: клиентам — базовые, администраторам — ещё и админские."""
    await bot.set_my_commands(CLIENT_COMMANDS)
    for admin_id in catalog.admin_ids():
        await set_admin_menu(bot, admin_id)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.callback_query.middleware(auto_answer_callback)
    dp.include_router(admin.router)  # раньше клиентского: там есть «ловушка» устаревших кнопок
    dp.include_router(router)
    await db.init_db()
    await catalog.init()
    await set_commands(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
