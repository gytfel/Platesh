"""Выдача покупки после оплаты: текст, файл, одноразовая ссылка в закрытый канал/чат."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import FSInputFile

from .config import Product
from .models import Invoice

if TYPE_CHECKING:
    from .bots.registry import BotRuntime


async def deliver(rt: BotRuntime, inv: Invoice, product: Product | None) -> None:
    """Отправляет покупателю то, за что он заплатил. Ошибки пробрасываются наружу."""
    if inv.user_id is None:
        return
    bot, chat_id = rt.bot, inv.user_id
    paid_text = rt.cfg.text("paid")
    if product is None:
        if paid_text:
            await bot.send_message(chat_id, paid_text)
        return

    d = product.delivery
    text = d.text or paid_text
    if text:
        await bot.send_message(chat_id, text)
    if d.file:
        document = FSInputFile(d.file) if Path(d.file).is_file() else d.file
        await bot.send_document(chat_id, document)
    if d.invite_chat_id is not None:
        link = await bot.create_chat_invite_link(
            d.invite_chat_id,
            name=f"platesh {inv.id}",
            member_limit=1,
            expire_date=int(time.time()) + d.invite_hours * 3600,
        )
        await bot.send_message(
            chat_id,
            rt.cfg.text("invite", hours=d.invite_hours, link=link.invite_link),
            disable_web_page_preview=True,
        )
