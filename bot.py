"""Telegram-бот для записи на процедуры в салон красоты (aiogram 3)."""
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
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

import db
from config import (
    ADMIN_IDS, BOT_TOKEN, DAYS_AHEAD, DAYS_OFF, MASTERS, SALON_ADDRESS, SALON_NAME,
    SALON_PHONE, SERVICES, SLOT_STEP_MIN, WORK_END, WORK_START,
)

router = Router()

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
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


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


CANCEL_ROW = [("✖️ Отмена", "abort")]


def services_kb() -> InlineKeyboardMarkup:
    rows = [[(f"{name} — {price} ₽", f"svc:{sid}")] for sid, (name, _, price) in SERVICES.items()]
    return inline(rows + [CANCEL_ROW])


def masters_kb(service_id: str) -> InlineKeyboardMarkup:
    rows = [[(name, f"mst:{mid}")] for mid, (name, skills) in MASTERS.items() if service_id in skills]
    return inline(rows + [[("⬅️ Назад", "back:service")], CANCEL_ROW])


def days_kb() -> InlineKeyboardMarkup:
    today = date.today()
    buttons = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        if d.weekday() in DAYS_OFF:
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
    duration = timedelta(minutes=SERVICES[service][1])
    busy = await db.busy_intervals(master, day.isoformat())
    now = datetime.now()
    t = datetime.combine(day, datetime.min.time()).replace(hour=WORK_START)
    close = t.replace(hour=WORK_END)
    slots = []
    while t + duration <= close:
        if t > now and all(not (t < e and t + duration > s) for s, e in busy):
            slots.append(t)
        t += timedelta(minutes=SLOT_STEP_MIN)
    return slots


def fmt_booking(row) -> str:
    start = datetime.fromisoformat(row["start_at"])
    return (f"<b>{SERVICES[row['service']][0]}</b>\n"
            f"🗓 {WEEKDAYS[start.weekday()]} {start:%d.%m.%Y}, {start:%H:%M}\n"
            f"👩 Мастер: {MASTERS[row['master']][0]}")


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:  # админ мог не запускать бота
            logging.warning("Не удалось уведомить админа %s: %s", admin_id, e)


# ---------- общие команды ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Здравствуйте! 👋 Я бот <b>{SALON_NAME}</b>.\n"
        "Помогу записаться на процедуру, посмотреть или отменить запись.",
        reply_markup=main_menu(),
    )


@router.message(F.text == BTN_CONTACTS)
async def contacts(message: Message):
    await message.answer(
        f"<b>{SALON_NAME}</b>\n📍 {SALON_ADDRESS}\n📞 {SALON_PHONE}\n"
        f"🕙 Ежедневно {WORK_START}:00–{WORK_END}:00"
        + (", кроме воскресенья" if 6 in DAYS_OFF else "")
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
    await state.set_state(Booking.service)
    await message.answer("Выберите услугу:", reply_markup=services_kb())


@router.callback_query(Booking.service, F.data.startswith("svc:"))
async def choose_service(call: CallbackQuery, state: FSMContext):
    service = call.data.split(":", 1)[1]
    await state.update_data(service=service)
    await state.set_state(Booking.master)
    name, minutes, price = SERVICES[service]
    await call.message.edit_text(
        f"Услуга: <b>{name}</b> ({minutes} мин, {price} ₽)\n\nВыберите мастера:",
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
    name, minutes, price = SERVICES[data["service"]]
    day = date.fromisoformat(data["day"])
    await message.answer("Почти готово!", reply_markup=main_menu())
    await message.answer(
        "Проверьте данные записи:\n\n"
        f"💅 Услуга: <b>{name}</b> ({minutes} мин)\n"
        f"👩 Мастер: {MASTERS[data['master']][0]}\n"
        f"🗓 {WEEKDAYS[day.weekday()]} {day:%d.%m.%Y}, {data['time']}\n"
        f"💰 Стоимость: {price} ₽\n"
        f"🙋 {data['name']}, {phone}",
        reply_markup=inline([[("✅ Подтвердить", "confirm")], CANCEL_ROW]),
    )


@router.callback_query(Booking.confirm, F.data == "confirm")
async def confirm(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    start = datetime.fromisoformat(f"{data['day']}T{data['time']}")
    end = start + timedelta(minutes=SERVICES[data["service"]][1])
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
        f"Ждём вас по адресу: {SALON_ADDRESS}\n"
        f"Посмотреть или отменить запись можно в разделе «{BTN_MY}»."
    )
    await notify_admins(bot, (
        f"🆕 Новая запись #{booking_id}\n"
        f"{SERVICES[data['service']][0]} — {MASTERS[data['master']][0]}\n"
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


# ---------- администратор ----------

@router.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def admin_schedule(message: Message):
    """Расписание на сегодня и завтра."""
    today = datetime.combine(date.today(), datetime.min.time())
    rows = await db.bookings_between(today, today + timedelta(days=2))
    if not rows:
        await message.answer("На сегодня и завтра записей нет.")
        return
    for row in rows:
        await message.answer(
            f"#{row['id']} {fmt_booking(row)}\n🙋 {row['client_name']}, {row['phone']}",
            reply_markup=inline([[("❌ Отменить", f"acancel:{row['id']}")]]),
        )


@router.callback_query(F.data.startswith("acancel:"), F.from_user.id.in_(ADMIN_IDS))
async def admin_cancel(call: CallbackQuery, bot: Bot):
    row = await db.cancel_booking(int(call.data.split(":", 1)[1]))
    if not row:
        await call.answer("Запись уже отменена", show_alert=True)
        return
    await call.message.edit_text(f"Запись #{row['id']} отменена администратором.")
    try:
        await bot.send_message(row["user_id"], "К сожалению, ваша запись отменена салоном. "
                                               f"Приносим извинения!\n\n{fmt_booking(row)}\n\n"
                                               f"Связаться с нами: {SALON_PHONE}")
    except Exception as e:
        logging.warning("Не удалось уведомить клиента: %s", e)


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


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.callback_query.middleware(auto_answer_callback)
    dp.include_router(router)
    await db.init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
