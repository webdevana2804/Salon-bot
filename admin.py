"""Админская часть: расписание (/admin) и настройки салона (/settings)."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import catalog
import config
import db
from common import WEEKDAYS, fmt_booking, fmt_days_off, fmt_hours, h, inline, set_admin_menu
from config import DAYS_AHEAD

router = Router()
# Все хендлеры этого роутера доступны только администраторам (список меняется на лету).
router.message.filter(lambda event: catalog.is_admin(event.from_user.id))
router.callback_query.filter(lambda event: catalog.is_admin(event.from_user.id))


class AdminInput(StatesGroup):
    value = State()  # ждём от админа текст; что именно — лежит в data["what"]


BACK_MENU = ("⬅️ В настройки", "st:menu")


# ---------- расписание ----------

@router.message(Command("admin"))
async def admin_schedule(message: Message):
    """Все предстоящие записи на DAYS_AHEAD дней вперёд, сгруппированные по дням."""
    now = datetime.now()
    until = datetime.combine(date.today() + timedelta(days=DAYS_AHEAD), datetime.min.time())
    rows = await db.bookings_between(now, until)
    if not rows:
        await message.answer(f"На ближайшие {DAYS_AHEAD} дней записей нет.")
        return
    by_day: dict[date, list] = {}
    for row in rows:
        by_day.setdefault(datetime.fromisoformat(row["start_at"]).date(), []).append(row)
    await message.answer(f"📋 Записи на {DAYS_AHEAD} дней вперёд: всего {len(rows)}")
    for day, day_rows in by_day.items():
        await message.answer(f"━━━━━━━━━━━━━━\n🗓 <b>{WEEKDAYS[day.weekday()]} {day:%d.%m}</b> — "
                             f"записей: {len(day_rows)}")
        for row in day_rows:
            start = datetime.fromisoformat(row["start_at"])
            await message.answer(
                f"<b>{start:%H:%M}</b> · {h(catalog.service_name(row['service']))} (#{row['id']})\n"
                f"👩 Мастер: {h(catalog.master_name(row['master']))}\n"
                f"🙋 {row['client_name']}, {row['phone']}",
                reply_markup=inline([[("❌ Отменить", f"acancel:{row['id']}")]]),
            )


@router.callback_query(F.data.startswith("acancel:"))
async def admin_cancel(call: CallbackQuery, bot: Bot):
    row = await db.cancel_booking(int(call.data.split(":", 1)[1]))
    if not row:
        await call.answer("Запись уже отменена", show_alert=True)
        return
    await call.message.edit_text(f"Запись #{row['id']} отменена администратором.")
    try:
        await bot.send_message(row["user_id"], "К сожалению, ваша запись отменена салоном. "
                                               f"Приносим извинения!\n\n{fmt_booking(row)}\n\n"
                                               f"Связаться с нами: {h(catalog.settings['phone'])}")
    except Exception as e:
        logging.warning("Не удалось уведомить клиента: %s", e)


# ---------- экраны настроек ----------

def menu_screen(user_id: int):
    s = catalog.settings
    text = ("⚙️ <b>Настройки салона</b>\n\n"
            f"🏠 Название: {h(s['salon_name'])}\n"
            f"📍 Адрес: {h(s['address'])}\n"
            f"📞 Телефон: {h(s['phone'])}\n"
            f"🕙 Часы работы: {fmt_hours()}\n"
            f"📅 Выходные: {fmt_days_off()}\n"
            f"💅 Услуг: {sum(x.active for x in catalog.services.values())} · "
            f"👩 Мастеров: {len(catalog.active_masters())}")
    kb = inline([
        [("📝 Приветствие", "st:greet")],
        [("🏠 Название", "st:ask:salon_name"), ("📍 Адрес", "st:ask:address")],
        [("📞 Телефон", "st:ask:phone"), ("🕙 Часы работы", "st:ask:hours")],
        [("📅 Выходные дни", "st:days")],
        [("💅 Услуги", "st:svcs"), ("👩 Мастера", "st:msts")],
        *([[("👑 Администраторы", "st:adm")]] if catalog.is_owner(user_id) else []),
        [("✖️ Закрыть", "st:close")],
    ])
    return text, kb


def admins_screen():
    lines = [f"👑 {uid} — главный (из файла .env)" for uid in sorted(config.ADMIN_IDS)]
    lines += [f"👤 {h(name) + ' · ' if name else ''}ID {uid}" for uid, name in catalog.extra_admins.items()]
    text = ("👑 <b>Администраторы</b>\n\n" + "\n".join(lines) + "\n\n"
            "Администраторы видят все записи, получают уведомления и меняют настройки. "
            "Назначать и снимать админов могут только главные.")
    rows = [[(f"🗑 Снять: {name or f'ID {uid}'}", f"st:admdel:{uid}")]
            for uid, name in catalog.extra_admins.items()]
    return text, inline(rows + [[("➕ Добавить администратора", "st:admadd")], [BACK_MENU]])


def days_screen():
    off = catalog.days_off()
    buttons = [(("🔴 " if d in off else "🟢 ") + WEEKDAYS[d], f"st:day:{d}") for d in range(7)]
    text = "📅 <b>Выходные дни</b>\n\nНажмите на день, чтобы сделать его рабочим 🟢 или выходным 🔴."
    return text, inline([buttons[:4], buttons[4:], [BACK_MENU]])


def services_screen():
    rows = []
    for s in catalog.services.values():
        if s.active:
            warn = "" if catalog.masters_for(s.id) else " ⚠️"
            rows.append([(f"{s.name} — {s.price} ₽{warn}", f"st:svc:{s.id}")])
    text = "💅 <b>Услуги</b>\n\nВыберите услугу для изменения или добавьте новую."
    if any(r[0][0].endswith("⚠️") for r in rows):
        text += "\n\n⚠️ — у услуги нет мастеров, клиенты её не видят."
    return text, inline(rows + [[("➕ Добавить услугу", "st:svcadd")], [BACK_MENU]])


def service_screen(sid: str):
    s = catalog.services[sid]
    names = ", ".join(h(m.name) for m in catalog.masters_for(sid))
    text = (f"💅 <b>{h(s.name)}</b>\n\n⏱ Длительность: {s.duration} мин\n💰 Цена: {s.price} ₽\n"
            f"👩 Мастера: {names or '⚠️ не выбраны — клиенты не видят услугу'}")
    return text, inline([
        [("✏️ Название", f"st:sf:name:{sid}"), ("⏱ Длительность", f"st:sf:duration:{sid}")],
        [("💰 Цена", f"st:sf:price:{sid}"), ("👩 Мастера", f"st:sm:{sid}")],
        [("🗑 Удалить услугу", f"st:sdel:{sid}")],
        [("⬅️ К услугам", "st:svcs")],
    ])


def service_masters_screen(sid: str):
    rows = [[(("✅ " if sid in m.services else "▫️ ") + m.name, f"st:lk:s:{m.id}:{sid}")]
            for m in catalog.active_masters()]
    text = (f"👩 Кто выполняет услугу «{h(catalog.services[sid].name)}»?\n"
            "Нажмите на мастера, чтобы отметить ✅ или снять отметку.")
    if not rows:
        text += "\n\nМастеров пока нет — добавьте их в разделе «👩 Мастера»."
    return text, inline(rows + [[("⬅️ К услуге", f"st:svc:{sid}")]])


def masters_screen():
    rows = [[(m.name, f"st:mst:{m.id}")] for m in catalog.active_masters()]
    text = "👩 <b>Мастера</b>\n\nВыберите мастера для изменения или добавьте нового."
    return text, inline(rows + [[("➕ Добавить мастера", "st:mstadd")], [BACK_MENU]])


def master_screen(mid: str):
    m = catalog.masters[mid]
    svc = ", ".join(h(catalog.services[s].name) for s in catalog.services if s in m.services)
    text = f"👩 <b>{h(m.name)}</b>\n\n💅 Услуги: {svc or 'не выбраны'}"
    return text, inline([
        [("✏️ Имя", f"st:mname:{mid}"), ("💅 Услуги", f"st:ms:{mid}")],
        [("🗑 Удалить мастера", f"st:mdel:{mid}")],
        [("⬅️ К мастерам", "st:msts")],
    ])


def master_services_screen(mid: str):
    m = catalog.masters[mid]
    rows = [[(("✅ " if s.id in m.services else "▫️ ") + s.name, f"st:lk:m:{mid}:{s.id}")]
            for s in catalog.services.values() if s.active]
    text = (f"💅 Какие услуги выполняет {h(m.name)}?\n"
            "Нажмите на услугу, чтобы отметить ✅ или снять отметку.")
    return text, inline(rows + [[("⬅️ К мастеру", f"st:mst:{mid}")]])


async def show(call: CallbackQuery, screen) -> None:
    text, kb = screen
    await call.message.edit_text(text, reply_markup=kb)


# ---------- навигация по настройкам ----------

@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext):
    await state.clear()
    text, kb = menu_screen(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "st:menu")
async def st_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, menu_screen(call.from_user.id))


@router.callback_query(F.data == "st:close")
async def st_close(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Настройки закрыты. Открыть снова: /settings")


@router.callback_query(F.data == "st:days")
async def st_days(call: CallbackQuery):
    await show(call, days_screen())


@router.callback_query(F.data.startswith("st:day:"))
async def st_toggle_day(call: CallbackQuery):
    day = int(call.data.rsplit(":", 1)[1])
    off = catalog.days_off() ^ {day}
    if len(off) == 7:
        await call.answer("Хотя бы один день должен быть рабочим", show_alert=True)
        return
    await catalog.set_setting("days_off", ",".join(str(d) for d in sorted(off)))
    await show(call, days_screen())


@router.callback_query(F.data == "st:svcs")
async def st_services(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, services_screen())


@router.callback_query(F.data.startswith("st:svc:"))
async def st_service(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, service_screen(call.data.split(":")[2]))


@router.callback_query(F.data.startswith("st:sm:"))
async def st_service_masters(call: CallbackQuery):
    await show(call, service_masters_screen(call.data.split(":")[2]))


@router.callback_query(F.data == "st:msts")
async def st_masters(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, masters_screen())


@router.callback_query(F.data.startswith("st:mst:"))
async def st_master(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, master_screen(call.data.split(":")[2]))


@router.callback_query(F.data.startswith("st:ms:"))
async def st_master_services(call: CallbackQuery):
    await show(call, master_services_screen(call.data.split(":")[2]))


@router.callback_query(F.data.startswith("st:lk:"))
async def st_toggle_link(call: CallbackQuery):
    _, _, back, mid, sid = call.data.split(":")
    await catalog.toggle_link(mid, sid)
    await show(call, service_masters_screen(sid) if back == "s" else master_services_screen(mid))


# ---------- удаление ----------

@router.callback_query(F.data.startswith("st:sdel:"))
async def st_service_delete_ask(call: CallbackQuery):
    sid = call.data.split(":")[2]
    n = await db.count_future("service", sid)
    note = f"\n\nПредстоящих записей на эту услугу: {n}. Они сохранятся." if n else ""
    await call.message.edit_text(
        f"Удалить услугу «{h(catalog.services[sid].name)}»? Клиенты больше не смогут на неё записаться.{note}",
        reply_markup=inline([[("🗑 Да, удалить", f"st:sdel!:{sid}"), ("Отмена", f"st:svc:{sid}")]]),
    )


@router.callback_query(F.data.startswith("st:sdel!:"))
async def st_service_delete(call: CallbackQuery):
    await catalog.delete_service(call.data.split(":")[2])
    await call.answer("Услуга удалена")
    await show(call, services_screen())


@router.callback_query(F.data.startswith("st:mdel:"))
async def st_master_delete_ask(call: CallbackQuery):
    mid = call.data.split(":")[2]
    n = await db.count_future("master", mid)
    note = f"\n\nПредстоящих записей к этому мастеру: {n}. Они сохранятся." if n else ""
    await call.message.edit_text(
        f"Удалить мастера «{h(catalog.masters[mid].name)}»? Клиенты больше не смогут записаться к этому мастеру.{note}",
        reply_markup=inline([[("🗑 Да, удалить", f"st:mdel!:{mid}"), ("Отмена", f"st:mst:{mid}")]]),
    )


@router.callback_query(F.data.startswith("st:mdel!:"))
async def st_master_delete(call: CallbackQuery):
    await catalog.delete_master(call.data.split(":")[2])
    await call.answer("Мастер удалён")
    await show(call, masters_screen())


# ---------- администраторы (только для главных) ----------

ADD_ADMIN_HELP = (
    "➕ <b>Новый администратор</b>\n\n"
    "Отправьте его Telegram ID — число, которое человек может узнать у @userinfobot.\n"
    "Или отправьте его контакт: 📎 → Контакт (если он есть у вас в телефоне и в Telegram).\n\n"
    "Важно: человек должен сам открыть этого бота и нажать Start — иначе бот не сможет ему писать."
)


@router.callback_query(F.data == "st:adm")
async def st_admins(call: CallbackQuery, state: FSMContext):
    if not catalog.is_owner(call.from_user.id):
        await call.answer("Это могут только главные администраторы", show_alert=True)
        return
    await state.clear()
    await show(call, admins_screen())


@router.callback_query(F.data == "st:admadd")
async def st_admin_add(call: CallbackQuery, state: FSMContext):
    if not catalog.is_owner(call.from_user.id):
        await call.answer("Это могут только главные администраторы", show_alert=True)
        return
    await state.set_state(AdminInput.value)
    await state.update_data(what="new_admin")
    await call.message.edit_text(ADD_ADMIN_HELP + CANCEL_HINT)


@router.callback_query(F.data.startswith("st:admdel:"))
async def st_admin_delete_ask(call: CallbackQuery):
    uid = int(call.data.split(":")[2])
    if not catalog.is_owner(call.from_user.id) or uid not in catalog.extra_admins:
        await call.answer("Недоступно", show_alert=True)
        return
    await call.message.edit_text(
        f"Снять права администратора с «{h(catalog.extra_admins[uid] or f'ID {uid}')}»?\n"
        "Этот человек перестанет видеть записи и настройки, но сможет записываться как клиент.",
        reply_markup=inline([[("🗑 Да, снять", f"st:admdel!:{uid}"), ("Отмена", "st:adm")]]),
    )


@router.callback_query(F.data.startswith("st:admdel!:"))
async def st_admin_delete(call: CallbackQuery, bot: Bot):
    uid = int(call.data.split(":")[2])
    if not catalog.is_owner(call.from_user.id):
        await call.answer("Недоступно", show_alert=True)
        return
    await catalog.remove_admin(uid)
    await set_admin_menu(bot, uid, is_admin=False)
    await call.answer("Права администратора сняты")
    await show(call, admins_screen())


async def add_admin_from(message: Message, bot: Bot) -> str | None:
    """Добавляет админа по ID из текста или по контакту. Возвращает ошибку или None."""
    if message.contact:
        if not message.contact.user_id:
            return "У этого контакта нет Telegram. Отправьте ID числом (его можно узнать у @userinfobot)."
        uid = message.contact.user_id
        name = " ".join(filter(None, [message.contact.first_name, message.contact.last_name]))
    else:
        text = (message.text or "").strip()
        if not text.isdigit() or not 5 <= len(text) <= 15:
            return "Отправьте Telegram ID числом, например <code>987654321</code>, или контакт человека."
        uid, name = int(text), ""
    if catalog.is_admin(uid):
        return "Этот человек уже администратор."
    reachable = True
    try:
        chat = await bot.get_chat(uid)
        name = name or " ".join(filter(None, [chat.first_name, chat.last_name])) or chat.username or ""
    except Exception:
        reachable = False  # человек ещё не нажимал Start в боте
    await catalog.add_admin(uid, name)
    await set_admin_menu(bot, uid)
    if reachable:
        try:
            await bot.send_message(uid, "👑 Вас назначили администратором салона в этом боте.\n\n"
                                        "/admin — записи на неделю\n/settings — услуги, мастера, тексты и график")
        except Exception:
            reachable = False
    return None if reachable else "not_reachable"


# ---------- ввод текста ----------

PROMPTS = {
    "greeting": "Отправьте новый текст приветствия. Можно использовать <b>жирный</b>, <i>курсив</i> и эмодзи — "
                "форматирование сохранится.",
    "salon_name": "Отправьте новое название салона.",
    "address": "Отправьте новый адрес салона.",
    "phone": "Отправьте новый телефон салона.",
    "hours": "Отправьте часы работы в формате <code>10-20</code> (с 10:00 до 20:00).",
    "svc_name": "Отправьте новое название услуги.",
    "svc_duration": "Отправьте длительность услуги в минутах, например <code>90</code>.",
    "svc_price": "Отправьте цену услуги в рублях, например <code>2500</code>.",
    "new_svc_name": "➕ Новая услуга. Шаг 1 из 3: отправьте название.",
    "new_svc_duration": "Шаг 2 из 3: отправьте длительность в минутах, например <code>60</code>.",
    "new_svc_price": "Шаг 3 из 3: отправьте цену в рублях, например <code>2000</code>.",
    "master_name": "Отправьте новое имя мастера.",
    "new_master": "➕ Отправьте имя нового мастера.",
}
CANCEL_HINT = "\n\nПередумали? Отправьте /cancel"


async def ask(call: CallbackQuery, state: FSMContext, what: str, **data) -> None:
    await state.set_state(AdminInput.value)
    await state.update_data(what=what, **data)
    extra = ""
    if what == "greeting":
        extra = f"\n\nСейчас клиенты видят:\n━━━━━━━━━━━━━━\n{catalog.settings['greeting']}\n━━━━━━━━━━━━━━"
    await call.message.edit_text(PROMPTS[what] + extra + CANCEL_HINT)


@router.callback_query(F.data == "st:greet")
async def st_ask_greeting(call: CallbackQuery, state: FSMContext):
    await ask(call, state, "greeting")


@router.callback_query(F.data.startswith("st:ask:"))
async def st_ask_setting(call: CallbackQuery, state: FSMContext):
    await ask(call, state, call.data.split(":")[2])


@router.callback_query(F.data.startswith("st:sf:"))
async def st_ask_service_field(call: CallbackQuery, state: FSMContext):
    _, _, column, sid = call.data.split(":")
    await ask(call, state, f"svc_{column}", sid=sid)


@router.callback_query(F.data == "st:svcadd")
async def st_ask_new_service(call: CallbackQuery, state: FSMContext):
    await ask(call, state, "new_svc_name")


@router.callback_query(F.data.startswith("st:mname:"))
async def st_ask_master_name(call: CallbackQuery, state: FSMContext):
    await ask(call, state, "master_name", mid=call.data.split(":")[2])


@router.callback_query(F.data == "st:mstadd")
async def st_ask_new_master(call: CallbackQuery, state: FSMContext):
    await ask(call, state, "new_master")


@router.message(AdminInput.value, Command("cancel"))
async def input_cancel(message: Message, state: FSMContext):
    await state.clear()
    text, kb = menu_screen(message.from_user.id)
    await message.answer("Изменение отменено.\n\n" + text, reply_markup=kb)


def parse_int(text: str, low: int, high: int) -> int | None:
    digits = re.sub(r"[\s₽рубмин.]", "", text.lower())
    if digits.isdigit() and low <= int(digits) <= high:
        return int(digits)
    return None


@router.message(AdminInput.value, F.contact)
@router.message(AdminInput.value, F.text)
async def input_value(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    what, text = data["what"], (message.text or "").strip()

    async def reply(screen, note: str = "✅ Сохранено") -> None:
        await state.clear()
        body, kb = screen
        await message.answer(f"{note}\n\n{body}", reply_markup=kb)

    async def retry(error: str) -> None:
        await message.answer(f"⚠️ {error}{CANCEL_HINT}")

    if what == "new_admin":
        if not catalog.is_owner(message.from_user.id):
            await state.clear()
            return
        error = await add_admin_from(message, bot)
        if error == "not_reachable":
            return await reply(admins_screen(),
                               "✅ Администратор добавлен.\n⚠️ Этот человек ещё не открывал бота. Попросите найти бота "
                               "и нажать Start, иначе уведомления о записях до него не дойдут.")
        if error:
            return await retry(error)
        return await reply(admins_screen(), "✅ Администратор добавлен, ему отправлено уведомление.")

    if message.contact:
        return await retry("Здесь нужен текст, а не контакт.")

    if what == "greeting":
        if len(text) > 1000:
            return await retry("Слишком длинный текст, уложитесь в 1000 символов.")
        await catalog.set_setting("greeting", message.html_text)
        return await reply(menu_screen(message.from_user.id), "✅ Приветствие обновлено. Проверьте: /start")

    if what in {"salon_name", "address", "phone"}:
        if not 2 <= len(text) <= 200:
            return await retry("Текст должен быть от 2 до 200 символов.")
        await catalog.set_setting(what, text)
        note = "✅ Сохранено"
        if what == "salon_name":
            note += "\nЕсли название упоминается в приветствии, обновите и его: «📝 Приветствие»."
        return await reply(menu_screen(message.from_user.id), note)

    if what == "hours":
        m = re.fullmatch(r"(\d{1,2})(?::00)?\s*[-–—]\s*(\d{1,2})(?::00)?", text)
        start, end = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        if not m or not 0 <= start < end <= 24:
            return await retry("Не понял формат. Пример: <code>10-20</code> — с 10:00 до 20:00.")
        await catalog.set_setting("work_start", str(start))
        await catalog.set_setting("work_end", str(end))
        return await reply(menu_screen(message.from_user.id))

    if what in {"svc_name", "new_svc_name", "master_name", "new_master"}:
        if not 2 <= len(text) <= 60:
            return await retry("Название должно быть от 2 до 60 символов.")
        if what == "svc_name":
            await catalog.update_service(data["sid"], "name", text)
            return await reply(service_screen(data["sid"]))
        if what == "master_name":
            await catalog.rename_master(data["mid"], text)
            return await reply(master_screen(data["mid"]))
        if what == "new_master":
            mid = await catalog.add_master(text)
            return await reply(master_services_screen(mid), "✅ Мастер добавлен. Отметьте его услуги:")
        await state.update_data(what="new_svc_duration", name=text)
        return await message.answer(PROMPTS["new_svc_duration"] + CANCEL_HINT)

    if what in {"svc_duration", "new_svc_duration"}:
        minutes = parse_int(text, 10, 600)
        if minutes is None:
            return await retry("Укажите число минут от 10 до 600, например <code>90</code>.")
        if what == "svc_duration":
            await catalog.update_service(data["sid"], "duration", minutes)
            return await reply(service_screen(data["sid"]))
        await state.update_data(what="new_svc_price", duration=minutes)
        return await message.answer(PROMPTS["new_svc_price"] + CANCEL_HINT)

    if what in {"svc_price", "new_svc_price"}:
        price = parse_int(text, 0, 1_000_000)
        if price is None:
            return await retry("Укажите цену числом, например <code>2500</code>.")
        if what == "svc_price":
            await catalog.update_service(data["sid"], "price", price)
            return await reply(service_screen(data["sid"]))
        sid = await catalog.add_service(data["name"], data["duration"], price)
        return await reply(service_masters_screen(sid),
                           "✅ Услуга добавлена. Отметьте мастеров, которые её выполняют — "
                           "без этого клиенты её не увидят:")


@router.message(AdminInput.value)
async def input_not_text(message: Message):
    await message.answer("Пожалуйста, отправьте текст." + CANCEL_HINT)
