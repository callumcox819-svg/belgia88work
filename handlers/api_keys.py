"""Ключ API и профиль для генерации ссылок (Бельгия)."""

from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest

from database import Session
from services.users import get_or_create_user
from services.aqua_keys import (
    AQUA_PROFILE_ADDRESS_KEY,
    AQUA_PROFILE_NAME_KEY,
    AQUA_PROFILE_TITLE_KEY,
    AQUA_USER_API_KEY_SETTING,
    apply_aqua_profile_to_user,
    get_global_aqua_team_key,
    get_user_aqua_api_keys_async,
    get_user_aqua_service,
    get_user_aqua_user_key_async,
    get_user_goo_profile_id,
    get_user_profile_address,
    get_user_profile_buyer_name,
    get_user_profile_title,
    normalize_aqua_api_key,
    user_profile_fields_complete,
)
from services.aqua_network import AquaError
from services.aqua_profiles import (
    AquaProfile,
    create_aqua_profile_remote,
    fetch_aqua_team_profiles,
    find_profile_in_list,
)
from services.user_settings import set_user_setting
from utils.secrets import clean_secret

router = Router(name="api_keys")


class KeysState(StatesGroup):
    waiting_value = State()


class ProfileState(StatesGroup):
    title = State()
    buyer_name = State()
    address = State()


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")]]
    )


def profile_screen_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Заполнить / изменить", callback_data="aqua_profile_create")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
            [InlineKeyboardButton(text="🟢 Скрыть", callback_data="aqua_hide")],
        ]
    )


def key_screen_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Установить ключ", callback_data="aqua_set:user_key")],
            [InlineKeyboardButton(text="🔍 Проверить ключ", callback_data="aqua_test_keys")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
            [InlineKeyboardButton(text="🟢 Скрыть", callback_data="aqua_hide")],
        ]
    )


def _show_full(key: str | None) -> str:
    return (key or "—").strip() or "—"


def _field_line(label: str, value: str) -> str:
    v = (value or "").strip() or "—"
    return f"{label}: <code>{v}</code>"


async def _render_profile_screen(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        title = await get_user_profile_title(session, user)
        buyer = await get_user_profile_buyer_name(session, user)
        addr = await get_user_profile_address(session, user)
        complete = await user_profile_fields_complete(session, user)
        status = "🟢 готов к генерации" if complete and get_user_goo_profile_id(user) else (
            "🟡 заполните все поля" if not complete else "🟡 сохранён, ждём profileID"
        )
        text = (
            "👤 <b>Профиль</b>\n\n"
            "Эти данные уходят в сгенерированную ссылку.\n\n"
            f"{_field_line('Название профиля', title)}\n"
            f"{_field_line('Имя получателя', buyer)}\n"
            f"{_field_line('Адрес доставки', addr)}\n\n"
            f"Статус: {status}"
        )
    try:
        await callback.message.edit_text(text, reply_markup=profile_screen_kb(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def _render_key_screen(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        user_key = await get_user_aqua_user_key_async(session, user)
    team_ok = bool(get_global_aqua_team_key())
    text = (
        "🔑 <b>Личный API-ключ</b>\n\n"
        f"Статус: {'✅ задан' if user_key else '❌ не задан'}\n"
        f"<code>{_show_full(user_key)}</code>\n\n"
        "<i>Ваш токен из панели (не «Токен команды»).</i>\n\n"
        f"Токен команды на сервере: {'✅' if team_ok else '❌'}"
    )
    await callback.message.edit_text(text, reply_markup=key_screen_kb(), parse_mode="HTML")


async def _sync_profile_to_api(session, user) -> str | None:
    """Сохранить profileID после create или поиска в команде."""
    title = await get_user_profile_title(session, user)
    buyer = await get_user_profile_buyer_name(session, user)
    addr = await get_user_profile_address(session, user)
    if not (title and buyer and addr):
        return None

    user_key, team_key = await get_user_aqua_api_keys_async(session, user)
    service = await get_user_aqua_service(session, user)
    chosen: AquaProfile | None = None
    api_note = ""

    if user_key and team_key:
        try:
            chosen = await create_aqua_profile_remote(
                user_api_key=user_key,
                team_api_key=team_key,
                title=title,
                buyer_name=buyer,
                address=addr,
                service=service or None,
            )
        except AquaError as e:
            api_note = str(e)[:200]
            try:
                profiles = await fetch_aqua_team_profiles(
                    user_api_key=user_key,
                    team_api_key=team_key,
                    service=service or None,
                )
                chosen = find_profile_in_list(profiles, title=title, buyer_name=buyer)
            except AquaError:
                pass

    if chosen:
        await apply_aqua_profile_to_user(session, user, chosen)
        return None

    await set_user_setting(session, user, AQUA_PROFILE_TITLE_KEY, title)
    await set_user_setting(session, user, AQUA_PROFILE_NAME_KEY, buyer)
    await set_user_setting(session, user, AQUA_PROFILE_ADDRESS_KEY, addr)
    return api_note or "profileID не получен — проверьте личный ключ"


@router.callback_query(F.data == "aqua_hide")
async def aqua_hide(callback: CallbackQuery) -> None:
    await callback.message.edit_text("✅ Скрыто.")
    await callback.answer()


@router.callback_query(F.data == "aqua_show:profile")
async def aqua_show_profile(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _render_profile_screen(callback)


@router.callback_query(F.data == "aqua_show:key")
async def aqua_show_key(callback: CallbackQuery) -> None:
    await callback.answer()
    await _render_key_screen(callback)


@router.callback_query(F.data == "aqua_profile_create")
async def aqua_profile_create(callback: CallbackQuery, state: FSMContext) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        cur_title = await get_user_profile_title(session, user) or "—"
        cur_buyer = await get_user_profile_buyer_name(session, user) or "—"
        cur_addr = await get_user_profile_address(session, user) or "—"

    await state.clear()
    await state.set_state(ProfileState.title)
    await callback.message.edit_text(
        "✏️ <b>Профиль</b>\n\n"
        f"Сейчас:\n"
        f"• Название: <code>{cur_title}</code>\n"
        f"• Имя получателя: <code>{cur_buyer}</code>\n"
        f"• Адрес: <code>{cur_addr}</code>\n\n"
        "Отправь <b>название профиля</b> одним сообщением.\n"
        "<i>Например: Anna</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="aqua_show:profile")]]
        ),
    )
    await callback.answer()


@router.message(ProfileState.title)
async def profile_title_step(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("❌ Название пустое.")
        return
    await state.update_data(title=title)
    await state.set_state(ProfileState.buyer_name)
    await message.answer(
        "Отправь <b>имя получателя</b> — оно будет на сгенерированной ссылке.\n"
        "<i>Например: Anna Johansen</i>",
        parse_mode="HTML",
    )


@router.message(ProfileState.buyer_name)
async def profile_buyer_step(message: Message, state: FSMContext) -> None:
    buyer = (message.text or "").strip()
    if not buyer:
        await message.answer("❌ Имя пустое.")
        return
    await state.update_data(buyer_name=buyer)
    await state.set_state(ProfileState.address)
    await message.answer(
        "Отправь <b>адрес доставки</b> одним сообщением.\n"
        "<i>Например: Belgia 88 dom 33 ylica sosal</i>",
        parse_mode="HTML",
    )


@router.message(ProfileState.address)
async def profile_address_step(message: Message, state: FSMContext) -> None:
    addr = (message.text or "").strip()
    if not addr:
        await message.answer("❌ Адрес пустой.")
        return
    data = await state.get_data()
    title = (data.get("title") or "").strip()
    buyer = (data.get("buyer_name") or "").strip()

    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        await set_user_setting(session, user, AQUA_PROFILE_TITLE_KEY, title)
        await set_user_setting(session, user, AQUA_PROFILE_NAME_KEY, buyer)
        await set_user_setting(session, user, AQUA_PROFILE_ADDRESS_KEY, addr)
        warn = await _sync_profile_to_api(session, user)
        await session.commit()

    await state.clear()
    if warn:
        await message.answer(
            f"✅ Профиль сохранён.\n\n⚠️ {warn}",
            reply_markup=profile_screen_kb(),
        )
    else:
        await message.answer("✅ Профиль сохранён и привязан.", reply_markup=profile_screen_kb())


@router.callback_query(F.data == "aqua_test_keys")
async def aqua_test_keys(callback: CallbackQuery) -> None:
    await callback.answer("Проверяю…")
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        user_key = await get_user_aqua_user_key_async(session, user)
        team_key = get_global_aqua_team_key()
        if not user_key:
            return await callback.message.answer("❌ Личный ключ не задан. ⚙️ → 🔑")
        if not team_key:
            return await callback.message.answer("❌ Токен команды не задан на сервере.")
        try:
            profiles = await fetch_aqua_team_profiles(
                user_api_key=user_key,
                team_api_key=team_key,
                service=await get_user_aqua_service(session, user) or None,
            )
        except AquaError as e:
            return await callback.message.answer(f"❌ API: {e}")
    await callback.message.answer(
        f"✅ Ключ работает. Профилей в команде: <b>{len(profiles)}</b>.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "aqua_set:user_key")
async def aqua_set_user_key_begin(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(KeysState.waiting_value)
    await callback.message.edit_text(
        "✍️ <b>Личный API-ключ</b>\n\n"
        "Поле <b>«Ваш токен»</b> в панели (не «Токен команды»).",
        reply_markup=_back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(KeysState.waiting_value)
async def keys_set_finish(message: Message, state: FSMContext) -> None:
    value = clean_secret((message.text or "").strip())
    if not value:
        await message.answer("❌ Пустое значение.")
        return

    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        value = normalize_aqua_api_key(value)
        user.goo_user_api_key_aqua = value
        await set_user_setting(session, user, AQUA_USER_API_KEY_SETTING, value)
        await session.commit()

    await state.clear()
    await message.answer("✅ Ключ сохранён.")
