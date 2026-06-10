from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import List, Optional, Tuple

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.exceptions import TelegramNetworkError

from sqlalchemy import select, func, delete
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import db_session
from models import EmailAccount, OfferEmail, Offer, User, Proxy

from services.mailing_send import (
    MAIL_FAST_PARALLEL_ACCOUNTS,
    MAIL_VERIFY_SENT,
    mailing_send_overall_timeout_sec,
    send_mailing_one,
    send_mailing_one_parallel,
)
from services.users import get_or_create_user
from services.user_settings import get_user_setting
from services.placeholders import apply_placeholders

from handlers.status import render_status_text, tg_answer_safe
from services.sender import (
    SMTP_TIMEOUT_SEC,
    normalize_send_error,
    is_smtp_timeout_error,
)
from services.smtp_block_control import mark_account_smtp_blocked
from services.smtp_account_check import is_account_no_access_error
from keyboards.main_menu import main_menu_kb

from services.sending_state import SendingState
from services.sending_state import get_state as _get_sending_state
from services.sending_state import set_state as _set_sending_state
from services.settings import load_timing
router = Router(name="send")
logger = logging.getLogger(__name__)

FAST_MAILING_KEY = "fast_mailing"


async def _user_fast_mailing_enabled(session: AsyncSession, user: User) -> bool:
    v = await get_user_setting(session, user, FAST_MAILING_KEY)
    return str(v or "").strip().lower() in {"1", "true", "yes", "on", "y"}


async def _edit_status_text(status_msg: Message, text: str, **kwargs) -> None:
    """edit_text принимает только InlineKeyboardMarkup, не ReplyKeyboardMarkup."""
    kwargs.pop("reply_markup", None)
    await status_msg.edit_text(text, **kwargs)


# ============================================================
# 🔒 Совместимость API состояния рассылки
#
# В проекте есть handlers/stopsend.py, который импортирует
# get_sending_state из handlers/send.py.
# При этом реальное хранилище состояния находится в services/sending_state.py
# и оно синхронное.
#
# Поэтому тут делаем тонкие обёртки:
# - get_sending_state(user_id) -> SendingState | None
# - set_sending_state(user_id, state=...) -> SendingState
#
# Никакой новой логики, только совместимость.
# ============================================================


def get_sending_state(user_id: int) -> Optional[SendingState]:
    return _get_sending_state(user_id)


def set_sending_state(user_id: int, state: Optional[SendingState] = None, **kwargs) -> SendingState:
    if state is not None:
        # сохранить все известные поля
        return _set_sending_state(user_id, **getattr(state, "__dict__", {}))
    return _set_sending_state(user_id, **kwargs)

# ==========================
# Константы
# ==========================

# SOCKS5 через PySocks — один глобальный lock в ProxySMTPContext; >1 только ждут в очереди.
SMTP_CONCURRENCY_WITH_PROXY = 1
SMTP_CONCURRENCY_NO_PROXY = 1

def mailing_send_timeouts() -> int:
    return mailing_send_overall_timeout_sec()

# user settings keys (уже используются в проекте)
from services.aqua_keys import AQUA_PROFILE_ADDRESS_KEY, AQUA_PROFILE_NAME_KEY


async def _safe_commit(session: AsyncSession):
    try:
        await session.commit()
    except OperationalError:
        await session.rollback()
        raise


async def _safe_rollback(session: AsyncSession):
    try:
        await session.rollback()
    except Exception:
        pass


async def _get_active_accounts(session: AsyncSession, user_id: int) -> List[EmailAccount]:
    rows = (
        await session.execute(
            select(EmailAccount).where(
                EmailAccount.user_id == user_id,
                # В текущей модели EmailAccount нет is_active.
                # Активность аккаунта хранится в поле status (см. handlers/accounts.py).
                EmailAccount.status == "active",
            )
        )
    ).scalars().all()
    return list(rows)


def _shuffle_rotation_accounts(accounts: List[EmailAccount]) -> List[EmailAccount]:
    out = list(accounts)
    random.shuffle(out)
    return out


def _remove_account_from_rotation(
    rotation_accounts: List[EmailAccount], account_id: int
) -> List[EmailAccount]:
    """Убрать ящик из SMTP-ротации (smtp_blocked — IMAP не трогаем)."""
    aid = int(account_id)
    return [a for a in rotation_accounts if int(a.id) != aid]


async def _get_targets(session: AsyncSession, user_id: int) -> List[OfferEmail]:
    """Targets are OfferEmail rows belonging to offers of this user."""
    rows = (
        await session.execute(
            select(OfferEmail)
            .join(Offer, Offer.id == OfferEmail.offer_id)
            .where(Offer.user_id == user_id)
            .options(selectinload(OfferEmail.offer))
            .order_by(OfferEmail.id.asc())
        )
    ).scalars().all()
    return list(rows)


async def _get_targets_count(session: AsyncSession, user_id: int) -> int:
    return (
        await session.execute(
            select(func.count(OfferEmail.id))
            .select_from(OfferEmail)
            .join(Offer, Offer.id == OfferEmail.offer_id)
            .where(Offer.user_id == user_id)
        )
    ).scalar() or 0


async def _purge_target(session: AsyncSession, user_id: int, offer_email_id: int):
    """Удаляем цель из очереди (чтобы больше не отправлять)."""
    try:
        await session.execute(
            delete(OfferEmail)
            .where(OfferEmail.id == offer_email_id)
            .where(OfferEmail.offer_id.in_(select(Offer.id).where(Offer.user_id == user_id)))
        )
        await _safe_commit(session)
    except Exception:
        await _safe_rollback(session)


async def _build_message_for_target(session: AsyncSession, tg_user_id: int, tgt: OfferEmail) -> Tuple[str, str]:
    """Return (subject, body) for a single OfferEmail target."""

    offer: Offer | None = getattr(tgt, "offer", None)

    from services.offer_storage import offer_effective_title

    item_title = offer_effective_title(offer)
    price = (getattr(offer, "price", "") or "").strip()
    link = (getattr(offer, "link", "") or "").strip()
    image_url = (getattr(offer, "photo", "") or "").strip()

    buyer_name = ""
    address = ""

    user = await get_or_create_user(session, tg_user_id)
    buyer_name = ((await get_user_setting(session, user, AQUA_PROFILE_NAME_KEY)) or "").strip()
    address = ((await get_user_setting(session, user, AQUA_PROFILE_ADDRESS_KEY)) or "").strip()

    ctx = {
        "ITEM_TITLE": item_title,
        "PRICE": price,
        "BUYER_NAME": buyer_name,
        "ADDRESS": address,
        "IMAGE_URL": image_url,
    }

    # Умные пресеты (случайный текст) → иначе «Первые смс» — как happy88
    base_text = ""
    try:
        from handlers.templates import pick_random_smart_preset

        base_text = await pick_random_smart_preset(tg_user_id, item_title)
    except Exception:
        base_text = ""
    if not (base_text or "").strip():
        try:
            from handlers.first_sms import pick_random_first_sms

            base_text = await pick_random_first_sms(tg_user_id, item_title)
        except Exception:
            base_text = ("Hello! Is this item still available? " + (item_title or "OFFER")).strip()

    body = apply_placeholders(base_text, link=link, ctx=ctx)
    from services.offer_text import ensure_item_title_in_body, trim_trailing_offer_title

    body = ensure_item_title_in_body(body, item_title)
    body = trim_trailing_offer_title(body, item_title)

    from services.subject_offer import subject_for_offer

    subject = subject_for_offer(item_title or "")

    return subject, body


@router.message(Command("send"))
@router.message(F.text == "▶️ Запустить рассылку")
async def send_cmd(message: Message):
    await start_sending(message)


async def start_sending(message: Message):
    tg_user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    status_msg = await message.answer("⏳ Проверяю очередь и аккаунты…", parse_mode="HTML")

    try:
        await _start_sending_inner(
            message=message,
            status_msg=status_msg,
            tg_user_id=tg_user_id,
            chat_id=chat_id,
            bot=bot,
        )
    except Exception:
        logger.exception("start_sending failed tg=%s", tg_user_id)
        try:
            await _edit_status_text(
                status_msg,
                "❌ Ошибка запуска рассылки. Попробуйте /send снова.",
            )
        except Exception:
            await tg_answer_safe(
                message,
                "❌ Ошибка запуска рассылки. Попробуйте /send снова.",
                reply_markup=main_menu_kb(tg_user_id),
            )


async def _start_sending_inner(
    *,
    message: Message,
    status_msg: Message,
    tg_user_id: int,
    chat_id: int,
    bot: Bot,
) -> None:
    async with db_session() as session:
        db_user = await get_or_create_user(session, int(tg_user_id))

        db_user_id = db_user.id
        accounts = await _get_active_accounts(session, db_user_id)

        accounts_total_db = (
            await session.execute(select(func.count(EmailAccount.id)).where(EmailAccount.user_id == db_user_id))
        ).scalar() or 0

        total_targets = await _get_targets_count(session, db_user_id)

        if not accounts:
            await _edit_status_text(
                status_msg,
                "❌ Нет активных аккаунтов.\nДобавьте почту в «Настройки → Аккаунты».",
            )
            return

        if total_targets <= 0:
            await _edit_status_text(
                status_msg,
                "❌ Очередь пуста — нет email в БД после валидации.",
            )
            return

        state = get_sending_state(tg_user_id)
        if state and getattr(state, "is_running", False):
            await _edit_status_text(status_msg, "⚠️ Рассылка уже запущена.")
            return

        from proxy_manager import is_socks5_proxy

        all_px = (
            await session.execute(select(Proxy).where(Proxy.user_id == db_user_id))
        ).scalars().all()
        socks_total = sum(1 for p in all_px if is_socks5_proxy(p))

        if socks_total <= 0:
            await _edit_status_text(
                status_msg,
                "❌ Нет SOCKS5 прокси. Добавьте socks5://… в «Прокси».",
            )
            return

    try:
        await _edit_status_text(
            status_msg,
            "⏳ Проверяю SOCKS5 (туннель + SMTP+STARTTLS)…\n"
            "<i>Это может занять 1–2 минуты.</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    from services.mailing_proxy_health import preflight_proxies_for_mailing

    async with db_session() as session:
        user_for_flags = await get_or_create_user(session, tg_user_id)
        fast_mailing = await _user_fast_mailing_enabled(session, user_for_flags)

    px_ok, px_summary, px_detail = await preflight_proxies_for_mailing(int(db_user_id))
    if not px_ok:
        try:
            await _edit_status_text(
                status_msg,
                "❌ <b>Рассылка не запущена</b>\n\n" + px_detail,
                parse_mode="HTML",
            )
        except Exception:
            await tg_answer_safe(
                message,
                "❌ Рассылка не запущена.\n\n" + px_detail,
                reply_markup=main_menu_kb(tg_user_id),
                parse_mode="HTML",
            )
        return

    sendable_px = int(px_summary.ok) + int(px_summary.unknown)

    async with db_session() as session:
        state = SendingState(
            user_id=tg_user_id,
            is_running=True,
            is_stopping=False,
            total_targets=total_targets,
            sent_count=0,
            failed_count=0,
            accounts_total=int(accounts_total_db),
            accounts_active=len(accounts),
            last_error="",
            last_status="FAST" if fast_mailing else "NORMAL",
            fast_mailing=fast_mailing,
        )
        set_sending_state(tg_user_id, state=state)

    from services.mailing_active_db import set_mailing_active

    await set_mailing_active(tg_user_id, active=True)

    from services.mailing_send import MAIL_SEND_RETRIES, MAIL_VERIFY_SENT
    from services.smtp_proxy_send import MAIL_SMTP_MAX_PROXIES, MAIL_SMTP_TIMEOUT_SEC

    fast_acc_label = (
        f"<b>{MAIL_FAST_PARALLEL_ACCOUNTS}</b>"
        if MAIL_FAST_PARALLEL_ACCOUNTS > 0
        else "<b>все active</b>"
    )
    proxy_mode = (
        f"⚡ <b>Фаст рассыл</b> — <b>параллельно</b> {fast_acc_label} ящиков за волну, "
        f"без пауз (🟢/🟡 SOCKS5, прокси не 🔴 на временный сбой)"
        if fast_mailing
        else f"🌐 все живые SOCKS5 (до <b>{MAIL_SMTP_MAX_PROXIES}</b> попыток на письмо)"
    )
    scheme_line = (
        f"Схема: <b>{fast_acc_label} ящиков × 1 адрес</b> одновременно · <b>без пауз</b>\n"
        if fast_mailing
        else "Схема: <b>1 ящик → 1 умный пресет → 1 адрес</b> → пауза MIN–MAX\n"
    )

    try:
        await _edit_status_text(
            status_msg,
            "✅ <b>Рассылка запущена</b>\n"
            f"В очереди: <b>{total_targets}</b> · ящиков active: <b>{len(accounts)}</b>\n"
            f"{px_detail}\n"
            f"В рассылке SOCKS5: <b>{sendable_px}</b> (🔴 не используются)\n"
            f"{proxy_mode}\n"
            f"{scheme_line}"
            f"Успех в /stat: <b>{'IMAP Sent' if MAIL_VERIFY_SENT else 'SMTP 250+NOOP'}</b>\n"
            f"Таймаут SOCKS5: <b>{MAIL_SMTP_TIMEOUT_SEC}</b> с · повторов <b>{MAIL_SEND_RETRIES}</b>\n\n"
            "<i>Таймаут при отправке ≠ мёртвый прокси: бот пробует другие из списка. "
            "🔴 только при явном падении туннеля.</i>\n"
            "<i>Ящик с Message blocked снимается с рассылки, IMAP остаётся.</i>",
            parse_mode="HTML",
        )
    except Exception:
        await tg_answer_safe(
            message,
            "✅ Рассылка запущена.",
            reply_markup=main_menu_kb(tg_user_id),
        )

    asyncio.create_task(
        _sending_loop(bot=bot, chat_id=chat_id, tg_user_id=tg_user_id)
    )


async def _notify_sending_finished(*, bot: Bot, chat_id: int, tg_user_id: int) -> None:
    """Отдельное сообщение по завершении рассылки (успех / стоп / сбой)."""
    state = get_sending_state(tg_user_id)
    if not state:
        return

    pending = 0
    try:
        async with db_session() as session:
            user = await get_or_create_user(session, tg_user_id)
            pending = int(await _get_targets_count(session, int(user.id)))
    except Exception:
        pass

    sent = int(state.sent_count)
    failed = int(state.failed_count)
    status = (state.last_status or "").upper()

    if state.is_stopping:
        title = "⏹ <b>Рассылка остановлена</b>"
    elif status == "DONE":
        title = "✅ <b>Рассылка завершена</b>"
    else:
        title = "⚠️ <b>Рассылка прервана</b>"

    inbox_hint = ""
    try:
        from services.incoming_mail_stats import build_incoming_breakdown, format_incoming_breakdown_html

        async with db_session() as session:
            user = await get_or_create_user(session, tg_user_id)
            br = await build_incoming_breakdown(session, int(user.id))
        if int(br.get("total") or 0) > 0:
            inbox_hint = format_incoming_breakdown_html(br)
    except Exception:
        pass

    text = (
        f"{title}\n\n"
        f"Отправлено (SMTP): <b>{sent}</b>\n"
        f"Ошибок отправки: <b>{failed}</b>\n"
        f"Email в очереди: <b>{pending}</b>"
        f"{inbox_hint}\n\n"
        f"<i>Проверьте у получателя и папку <b>Спам</b>. /imap_diag — отбои vs ответы.</i>"
    )
    if failed > 0 and (state.last_error or "").strip() not in ("", "-"):
        who = f"\nПоследний адрес: <code>{state.last_failed_to}</code>" if state.last_failed_to else ""
        from handlers.status import _humanize_send_error

        text += f"\n\n{_humanize_send_error(normalize_send_error(state.last_error))}{who}"

    try:
        await bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=main_menu_kb(tg_user_id),
        )
    except Exception:
        logger.exception("failed to send mailing finished notification user=%s", tg_user_id)


async def _handle_send_failure(
    *,
    session: AsyncSession,
    db_user_id: int,
    state: SendingState,
    tgt: OfferEmail,
    err: str,
    acc: EmailAccount,
    bot: Bot | None = None,
    chat_id: int | None = None,
) -> bool:
    """Возвращает True, если ящик снят с SMTP (smtp_blocked) — убрать из ротации."""
    err = normalize_send_error(err)
    state.failed_count += 1
    state.last_error = err or "UNKNOWN"
    state.last_failed_to = (tgt.email or "").strip()

    if await mark_account_smtp_blocked(
        session,
        acc,
        err,
        db_user_id=db_user_id,
        bot=bot,
        chat_id=chat_id,
    ):
        return True

    if is_account_no_access_error(err):
        try:
            await session.delete(acc)
            await session.commit()
            logger.warning("Deleted account (no access): %s", acc.email)
        except Exception:
            await session.rollback()
        return True

  # Ошибки прокси/таймаут — адрес остаётся в очереди (повтор на следующих кругах).
    err_u = (err or "").upper()
    if (
        "RECIPIENT_DEAD" in err_u
        or "5.1.1" in err_u
        or "5.5.0" in err_u
        or "MAILBOX UNAVAILABLE" in err_u
        or "ADDRESS NOT FOUND" in err_u
    ):
        await _purge_target(session, db_user_id, int(tgt.id))
    return False


async def _sending_loop(*, bot: Bot, chat_id: int, tg_user_id: int) -> None:
    state = get_sending_state(tg_user_id) or SendingState(user_id=tg_user_id)
    smtp_sem = asyncio.Semaphore(SMTP_CONCURRENCY_WITH_PROXY)
    entered_main_loop = False
    acc_idx = 0
    rotation_accounts: List[EmailAccount] = []
    account_send_counts: dict[int, int] = {}

    async with db_session() as session:
        user = await get_or_create_user(session, tg_user_id)
        db_user_id = user.id

        rotation_accounts = _shuffle_rotation_accounts(
            await _get_active_accounts(session, db_user_id)
        )
        if not rotation_accounts:
            state.is_running = False
            state.last_error = "NO_ACCOUNTS|no_accounts|No active accounts"
            set_sending_state(tg_user_id, state=state)
            try:
                await bot.send_message(chat_id, "❌ Рассылка остановлена: нет активных аккаунтов.")
            except Exception:
                pass
            return

        from services.smtp_proxy_send import _list_active_socks5_proxies

        if not await _list_active_socks5_proxies(session, int(db_user_id)):
            state.is_running = False
            state.last_error = "PROXY_ERROR|no_active_proxy|No sendable SOCKS5"
            set_sending_state(tg_user_id, state=state)
            try:
                await bot.send_message(
                    chat_id,
                    "❌ Рассылка остановлена: нет 🟢/🟡 SOCKS5 (все 🔴). "
                    "Проверьте прокси в «Прокси».",
                )
            except Exception:
                pass
            return

    fast_mailing_loop = bool(getattr(state, "fast_mailing", False))
    send_one_timeout = mailing_send_overall_timeout_sec(fast_mailing=fast_mailing_loop)
    mail_max_per_account = max(0, int(os.getenv("MAIL_MAX_PER_ACCOUNT", "0")))

    def _status_after_send() -> str:
        return "FAST" if fast_mailing_loop else "NORMAL"

    try:
        while True:
            await asyncio.sleep(0)
            entered_main_loop = True
            state = get_sending_state(tg_user_id) or state
            if state.is_stopping:
                state.is_running = False
                set_sending_state(tg_user_id, state=state)
                break

            async with db_session() as session:
                user = await get_or_create_user(session, tg_user_id)
                db_user_id = int(user.id)
                sender_name = getattr(user, "sender_name", None)

                if not rotation_accounts:
                    rotation_accounts = _shuffle_rotation_accounts(
                        await _get_active_accounts(session, db_user_id)
                    )
                if not rotation_accounts:
                    state.is_running = False
                    state.last_status = "STOPPED"
                    set_sending_state(tg_user_id, state=state)
                    break

                remaining = await _get_targets_count(session, db_user_id)
                if remaining <= 0:
                    state.is_running = False
                    state.last_status = "DONE"
                    set_sending_state(tg_user_id, state=state)
                    break

                targets = await _get_targets(session, db_user_id)
                if not targets:
                    state.is_running = False
                    state.last_status = "DONE"
                    set_sending_state(tg_user_id, state=state)
                    break

                timing = await load_timing(session, tg_user_id)
                min_delay = float(timing.get("min_delay", 2.0))
                max_delay = float(timing.get("max_delay", 4.0))
                fast_mailing = fast_mailing_loop

                if mail_max_per_account > 0:
                    eligible_accounts = [
                        a
                        for a in rotation_accounts
                        if account_send_counts.get(int(a.id), 0) < mail_max_per_account
                    ]
                else:
                    eligible_accounts = list(rotation_accounts)

                if fast_mailing:
                    if not eligible_accounts:
                        state.is_running = False
                        state.last_error = (
                            "ACCOUNT_RATE_LIMIT|cap|All accounts hit MAIL_MAX_PER_ACCOUNT"
                        )
                        set_sending_state(tg_user_id, state=state)
                        try:
                            await bot.send_message(
                                chat_id,
                                f"⏹ Лимит писем с ящика ({mail_max_per_account}) за этот запуск.",
                            )
                        except Exception:
                            pass
                        break

                    wave_cap = (
                        MAIL_FAST_PARALLEL_ACCOUNTS
                        if MAIL_FAST_PARALLEL_ACCOUNTS > 0
                        else len(eligible_accounts)
                    )
                    wave_n = min(len(targets), len(eligible_accounts), wave_cap)
                    wave_accounts = [
                        eligible_accounts[(acc_idx + i) % len(eligible_accounts)]
                        for i in range(wave_n)
                    ]
                    wave_targets = targets[:wave_n]
                    acc_idx = (acc_idx + wave_n) % max(1, len(eligible_accounts))

                    prepared: list[
                        tuple[EmailAccount, OfferEmail, str, str]
                    ] = []
                    for acc_w, tgt_w in zip(wave_accounts, wave_targets):
                        subj_w, body_w = await _build_message_for_target(
                            session, tg_user_id, tgt_w
                        )
                        prepared.append((acc_w, tgt_w, subj_w, body_w))

                    preview = ", ".join(
                        (a.email or "").split("@")[0] for a in wave_accounts[:4]
                    )
                    if wave_n > 4:
                        preview += f" +{wave_n - 4}"
                    state.current_to = f"⚡ {wave_n} ящиков: {preview}"
                    state.last_status = "FAST"
                    set_sending_state(tg_user_id, state=state)

                    logger.info(
                        "[mailing fast wave] accounts=%s targets=%s",
                        wave_n,
                        [t.email for t in wave_targets[:5]],
                    )

                    async def _parallel_send(
                        acc_w: EmailAccount,
                        tgt_w: OfferEmail,
                        subj_w: str,
                        body_w: str,
                    ) -> Tuple[bool, str, EmailAccount, OfferEmail]:
                        to_w = (tgt_w.email or "").strip()
                        try:
                            async with db_session() as ws:
                                ok_w, err_w, _ = await asyncio.wait_for(
                                    send_mailing_one_parallel(
                                        ws,
                                        db_user_id,
                                        acc_w,
                                        to_w,
                                        subj_w,
                                        body_w,
                                        sender_name=sender_name,
                                    ),
                                    timeout=send_one_timeout,
                                )
                            return (
                                bool(ok_w),
                                normalize_send_error(err_w),
                                acc_w,
                                tgt_w,
                            )
                        except asyncio.TimeoutError:
                            return (
                                False,
                                normalize_send_error(
                                    f"SMTP_TIMEOUT|timeout|exceeded {send_one_timeout}s"
                                ),
                                acc_w,
                                tgt_w,
                            )
                        except Exception as ex:
                            return (
                                False,
                                normalize_send_error(str(ex)),
                                acc_w,
                                tgt_w,
                            )

                    wave_raw = await asyncio.gather(
                        *[
                            _parallel_send(acc_w, tgt_w, subj_w, body_w)
                            for acc_w, tgt_w, subj_w, body_w in prepared
                        ]
                    )

                    state.current_to = ""
                    wave_ok = 0
                    for ok_w, err_w, acc_w, tgt_w in wave_raw:
                        if ok_w:
                            state.sent_count += 1
                            wave_ok += 1
                            account_send_counts[int(acc_w.id)] = (
                                account_send_counts.get(int(acc_w.id), 0) + 1
                            )
                            await _purge_target(session, db_user_id, tgt_w.id)
                        else:
                            blocked = await _handle_send_failure(
                                session=session,
                                db_user_id=db_user_id,
                                state=state,
                                tgt=tgt_w,
                                err=err_w or "UNKNOWN",
                                acc=acc_w,
                                bot=bot,
                                chat_id=chat_id,
                            )
                            if blocked:
                                rotation_accounts = _remove_account_from_rotation(
                                    rotation_accounts, int(acc_w.id)
                                )
                                state.accounts_active = len(rotation_accounts)

                    state.last_status = _status_after_send()
                    if wave_ok:
                        state.fast_proxy_fail_streak = 0
                    set_sending_state(tg_user_id, state=state)

                    if not state.is_running:
                        break
                    continue

                # Обычный режим: 1 ящик → 1 адрес → пауза MIN–MAX.
                acc: EmailAccount | None = None
                if not eligible_accounts:
                    state.is_running = False
                    state.last_error = (
                        "ACCOUNT_RATE_LIMIT|cap|All accounts hit MAIL_MAX_PER_ACCOUNT"
                    )
                    set_sending_state(tg_user_id, state=state)
                    try:
                        await bot.send_message(
                            chat_id,
                            f"⏹ Лимит писем с ящика ({mail_max_per_account}) за этот запуск. "
                            "Запустите рассылку снова позже.",
                        )
                    except Exception:
                        pass
                    break
                acc = eligible_accounts[acc_idx % len(eligible_accounts)]
                acc_idx += 1

                tgt = targets[0]
                to_addr = (tgt.email or "").strip()
                state.current_to = to_addr
                state.last_status = "SENDING"
                set_sending_state(tg_user_id, state=state)

                subject, body = await _build_message_for_target(
                    session, tg_user_id, tgt
                )
                logger.info(
                    "[mailing rotate] from=%s to=%s subject=%r",
                    acc.email,
                    to_addr,
                    (subject or "")[:60],
                )
                try:
                    async with smtp_sem:
                        ok, err, _msgid = await asyncio.wait_for(
                            send_mailing_one(
                                session,
                                db_user_id,
                                acc,
                                to_addr,
                                subject,
                                body,
                                sender_name=sender_name,
                                fast_mailing=False,
                            ),
                            timeout=send_one_timeout,
                        )
                except asyncio.TimeoutError:
                    ok, err = False, normalize_send_error(
                        f"SMTP_TIMEOUT|timeout|SMTP send exceeded {send_one_timeout}s"
                    )
                except Exception as e:
                    ok, err = False, normalize_send_error(str(e))

                state.current_to = ""
                if ok:
                    state.sent_count += 1
                    state.last_status = _status_after_send()
                    account_send_counts[int(acc.id)] = account_send_counts.get(
                        int(acc.id), 0
                    ) + 1
                    await _purge_target(session, db_user_id, tgt.id)
                else:
                    state.last_status = _status_after_send()
                    blocked = await _handle_send_failure(
                        session=session,
                        db_user_id=db_user_id,
                        state=state,
                        tgt=tgt,
                        err=err or "UNKNOWN",
                        acc=acc,
                        bot=bot,
                        chat_id=chat_id,
                    )
                    if blocked:
                        rotation_accounts = _remove_account_from_rotation(
                            rotation_accounts, int(acc.id)
                        )
                        state.accounts_active = len(rotation_accounts)
                        logger.info(
                            "removed smtp_blocked account from rotation: %s",
                            acc.email,
                        )

                set_sending_state(tg_user_id, state=state)

            if not state.is_running:
                break
            if not fast_mailing_loop:
                await asyncio.sleep(random.uniform(min_delay, max_delay))

    except TelegramNetworkError:
        state.is_running = False
        state.last_error = "TG_ERROR|network|Telegram network error"
        set_sending_state(tg_user_id, state=state)
    except Exception as e:
        state.is_running = False
        state.last_error = normalize_send_error(str(e))
        set_sending_state(tg_user_id, state=state)
        logger.exception("sending loop failed for user %s", tg_user_id)
    finally:
        state = get_sending_state(tg_user_id) or state
        if state.is_running:
            state.is_running = False
            set_sending_state(tg_user_id, state=state)
        try:
            from services.mailing_active_db import set_mailing_active

            await set_mailing_active(tg_user_id, active=False)
        except Exception:
            logger.exception("clear mailing_active flag tg=%s", tg_user_id)
        if entered_main_loop:
            await _notify_sending_finished(bot=bot, chat_id=chat_id, tg_user_id=tg_user_id)
