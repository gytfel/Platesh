"""Доступы и подписки: выдача/продление, напоминания, удаление из закрытых чатов, охрана входа."""

from __future__ import annotations

import html
import logging
import time
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Product
from .db import Database
from .models import Access
from .notifier import Notifier
from .texts import format_date

if TYPE_CHECKING:
    from .bots.registry import BotRegistry, BotRuntime

log = logging.getLogger(__name__)

DAY = 86400
REMIND_BEFORE = 3 * DAY
MEMBER_STATUSES = ("member", "administrator", "creator")


def remind_window(product: Product) -> int:
    """За сколько секунд до конца напоминать: 3 дня, но не больше трети срока (для коротких подписок)."""
    if product.days is None:  # срок задан вручную через /grant — напоминаем за стандартные 3 дня
        return REMIND_BEFORE
    return min(REMIND_BEFORE, product.days * DAY // 3)


def renew_markup(rt: BotRuntime, product: Product) -> InlineKeyboardMarkup:
    # Кнопку managed-бота обрабатывает витрина Platesh, у external-бота — роутер из platesh_client.
    data = f"p:{product.id}" if rt.cfg.managed else f"platesh:buy:{product.id}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=rt.cfg.text("renew_button"), callback_data=data)]])


class AccessService:
    def __init__(self, db: Database, registry: BotRegistry, notifier: Notifier) -> None:
        self.db = db
        self.registry = registry
        self.notifier = notifier

    # ------------------------------------------------------------------ выдача и проверка

    async def grant(self, rt: BotRuntime, user_id: int, product: Product, *, days: int | None = None) -> tuple[Access, bool]:
        """Выдаёт или продлевает доступ. Возвращает (доступ, был ли он активен до этого).

        Продление считается от текущей даты окончания, а не от сегодняшнего дня,
        поэтому оплатить заранее не означает потерять оставшиеся дни.
        """
        now = int(time.time())
        days = product.days if days is None else days
        current = await self.db.get_access(rt.name, user_id, product.id)
        was_active = current is not None and current.is_active(now)
        if days is None or (was_active and current.expires_at is None):
            expires_at = None
        else:
            start = current.expires_at if was_active else now
            expires_at = start + days * DAY
        access = Access(bot=rt.name, user_id=user_id, product_id=product.id, expires_at=expires_at)
        await self.db.save_access(access)
        log.info("Бот «%s»: доступ %s для %s — %s", rt.name, product.id, user_id,
                 f"до {format_date(expires_at)}" if expires_at else "бессрочно")
        return access, was_active

    async def revoke(self, rt: BotRuntime, user_id: int, product_id: str) -> bool:
        """Закрывает доступ и сразу удаляет из чата (если нет другого оплаченного доступа туда)."""
        current = await self.db.get_access(rt.name, user_id, product_id)
        if current is None or not current.is_active():
            return False
        current.revoked = True
        current.expires_at = int(time.time())
        await self.db.save_access(current)
        await self._remove_from_chat(rt, current)
        return True

    async def active_accesses(self, rt: BotRuntime, user_id: int) -> list[Access]:
        return [a for a in await self.db.user_accesses(rt.name, user_id) if a.is_active()]

    async def has_chat_access(self, rt: BotRuntime, user_id: int, chat_id: int) -> bool:
        if rt.is_admin(user_id):
            return True
        products = {p.id for p in rt.cfg.chat_products(chat_id)}
        return any(a.product_id in products for a in await self.active_accesses(rt, user_id))

    # ------------------------------------------------------------------ закрытые чаты

    async def kick(self, rt: BotRuntime, chat_id: int, user_id: int) -> None:
        """Удаляет из чата, но не оставляет в бане — после оплаты можно вернуться."""
        await rt.bot.ban_chat_member(chat_id, user_id)
        await rt.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)

    async def is_member(self, rt: BotRuntime, chat_id: int, user_id: int) -> bool:
        try:
            member = await rt.bot.get_chat_member(chat_id, user_id)
        except TelegramAPIError:
            return False
        return member.status in MEMBER_STATUSES or (member.status == "restricted" and getattr(member, "is_member", False))

    async def _remove_from_chat(self, rt: BotRuntime, access: Access) -> None:
        product = rt.cfg.products.get(access.product_id)
        chat_id = product.chat_id if product else None
        if chat_id is None or await self.has_chat_access(rt, access.user_id, chat_id):
            return
        try:
            member = await rt.bot.get_chat_member(chat_id, access.user_id)
            if member.status in ("left", "kicked", "administrator", "creator"):
                return
            await self.kick(rt, chat_id, access.user_id)
            log.info("Бот «%s»: %s удалён из чата %s (доступ %s закончился)", rt.name, access.user_id, chat_id, access.product_id)
        except TelegramAPIError as exc:
            log.warning("Бот «%s»: не удалось удалить %s из чата %s: %s", rt.name, access.user_id, chat_id, exc)
            await self.notifier.alert(
                rt,
                f"⚠️ Доступ <code>{access.user_id}</code> к «{html.escape(product.title)}» закончился, "
                f"но удалить его из чата <code>{chat_id}</code> не удалось: <code>{html.escape(exc.message)}</code>. "
                f"Проверь, что бот — админ с правом удалять участников.",
            )

    # ------------------------------------------------------------------ фоновая проверка сроков

    async def _send(self, rt: BotRuntime, user_id: int, text: str, product: Product | None) -> None:
        try:
            await rt.bot.send_message(user_id, text, reply_markup=renew_markup(rt, product) if product else None)
        except TelegramAPIError:
            pass  # пользователь заблокировал бота — ничего страшного

    async def process_expirations(self) -> None:
        now = int(time.time())
        for access in await self.db.expiring_accesses(now + REMIND_BEFORE):
            rt = self.registry.get(access.bot)
            product = rt.cfg.products.get(access.product_id) if rt else None
            if product is None or access.expires_at <= now:
                continue
            if access.expires_at - now > remind_window(product):
                continue
            access.reminded = True
            await self.db.save_access(access)
            text = rt.cfg.text("sub_reminder", title=html.escape(product.title), date=format_date(access.expires_at))
            await self._send(rt, access.user_id, text, product)

        for access in await self.db.expired_accesses(now):
            rt = self.registry.get(access.bot)
            if rt is None:
                continue  # бот убран из конфига — разберёмся, когда вернут
            access.revoked = True
            await self.db.save_access(access)
            product = rt.cfg.products.get(access.product_id)
            await self._remove_from_chat(rt, access)
            title = html.escape(product.title if product else access.product_id)
            await self._send(rt, access.user_id, rt.cfg.text("sub_expired", title=title), product)
            self.notifier.access_expired(rt, access)


async def diagnose(rt: BotRuntime) -> list[str]:
    """Проверяет закрытые чаты бота: бот — админ, чат приватный, копирование запрещено."""
    problems = []
    for chat_id in sorted(rt.cfg.chats()):
        try:
            chat = await rt.bot.get_chat(chat_id)
            me = await rt.bot.get_chat_member(chat_id, rt.id)
        except TelegramAPIError as exc:
            problems.append(f"чат {chat_id}: бот не имеет к нему доступа ({exc.message}) — добавь бота администратором")
            continue
        name = chat.title or str(chat_id)
        if me.status not in ("administrator", "creator"):
            problems.append(f"«{name}»: бот не администратор — не сможет выдавать ссылки и удалять неплательщиков")
            continue
        if not getattr(me, "can_invite_users", True):
            problems.append(f"«{name}»: у бота нет права «Пригласительные ссылки»")
        if chat.type != "channel" and not getattr(me, "can_restrict_members", True):
            problems.append(f"«{name}»: у бота нет права «Блокировка участников» — не сможет удалять неплательщиков")
        if chat.username:
            problems.append(f"«{name}» публичный (@{chat.username}): его читают без оплаты. Сделай его приватным")
        if not chat.has_protected_content:
            problems.append(f"«{name}»: не включён запрет копирования — посты можно пересылать и сохранять "
                            f"(настройки чата → «Запретить копирование»)")
    return problems
