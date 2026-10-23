"""Выдача покупки после оплаты: текст, файл, личная ссылка в закрытый канал/чат."""

from __future__ import annotations

import html
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import FSInputFile

from .config import Product
from .models import Access
from .texts import format_date

if TYPE_CHECKING:
    from .access import AccessService
    from .bots.registry import BotRuntime


async def deliver(
    rt: BotRuntime,
    access_service: AccessService,
    user_id: int | None,
    product: Product | None,
    *,
    access: Access | None = None,
    renewed: bool = False,
    tag: str = "",
) -> None:
    """Отправляет покупателю то, за что он заплатил. Ошибки пробрасываются наружу."""
    if user_id is None:
        return
    bot = rt.bot
    paid_text = rt.cfg.text("paid")
    if product is None:
        if paid_text:
            await bot.send_message(user_id, paid_text)
        return

    d = product.delivery
    # protect_content: сообщение и файл нельзя переслать, скопировать или сохранить.
    protect = d.protect_content
    text = d.text or paid_text
    if access is not None and access.expires_at is not None:
        date = format_date(access.expires_at)
        line = rt.cfg.text("access_extended", title=html.escape(product.title), date=date) if renewed \
            else rt.cfg.text("access_until", date=date)
        text = f"{text}\n\n{line}" if text else line
    if text:
        await bot.send_message(user_id, text, protect_content=protect)
    if d.file:
        document = FSInputFile(d.file) if Path(d.file).is_file() else d.file
        await bot.send_document(user_id, document, protect_content=protect)
    if d.invite_chat_id is not None and not await access_service.is_member(rt, d.invite_chat_id, user_id):
        await send_invite(rt, user_id, product, tag=tag)


async def send_invite(rt: BotRuntime, user_id: int, product: Product, *, tag: str = "") -> None:
    d = product.delivery
    expire_date = int(time.time()) + d.invite_hours * 3600
    name = f"platesh {tag or user_id}"[:32]
    if rt.cfg.managed:
        # Вход по заявке: Platesh одобрит её, только если у человека оплачен доступ.
        # Даже если ссылку перешлют, чужая заявка будет отклонена.
        link = await rt.bot.create_chat_invite_link(
            d.invite_chat_id, name=name, expire_date=expire_date, creates_join_request=True
        )
        hint = rt.cfg.text("invite_request_hint")
    else:
        # Боты со своим кодом получают заявки сами, поэтому для них — одноразовая ссылка.
        link = await rt.bot.create_chat_invite_link(d.invite_chat_id, name=name, expire_date=expire_date, member_limit=1)
        hint = ""
    await rt.bot.send_message(
        user_id,
        rt.cfg.text("invite", hours=d.invite_hours, link=link.invite_link) + hint,
        disable_web_page_preview=True,
        protect_content=d.protect_content,
    )
