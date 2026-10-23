"""Команды администратора внутри каждого бота: статистика, платежи, возвраты."""

from __future__ import annotations

import html
from datetime import datetime
from decimal import Decimal

from aiogram import Router
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import Message

from ..models import PENDING, Invoice
from ..payments import PaymentError, PaymentService
from ..texts import format_price
from .registry import BotRuntime

STATUS_ICONS = {"paid": "✅", "pending": "⏳", "expired": "⌛", "refunded": "↩️", "failed": "❌"}

HELP = (
    "<b>Команды администратора</b>\n\n"
    "/stats — выручка и покупатели\n"
    "/payments — последние оплаты\n"
    "/invoice <code>ID</code> — подробности счёта\n"
    "/refund <code>ID</code> — вернуть оплату Stars"
)


class IsAdmin(Filter):
    async def __call__(self, message: Message, rt: BotRuntime) -> bool:
        return rt.is_admin(message.from_user.id if message.from_user else None)


def _revenue_lines(rows: list[tuple[str, int, Decimal]]) -> str:
    if not rows:
        return "  —"
    return "\n".join(f"  {format_price(total, currency)} · {count} опл." for currency, count, total in rows)


def _invoice_line(inv: Invoice) -> str:
    date = datetime.fromtimestamp(inv.paid_at or inv.created_at).strftime("%d.%m %H:%M")
    who = f" · <code>{inv.user_id}</code>" if inv.user_id else ""
    return f"{STATUS_ICONS.get(inv.status, '')} {date} · {html.escape(inv.title)} · {inv.price}{who}\n   <code>{inv.id}</code>"


def create_admin_router() -> Router:
    router = Router(name="admin")
    router.message.filter(IsAdmin())

    @router.message(Command("admin"))
    async def admin_help(message: Message) -> None:
        await message.answer(HELP)

    @router.message(Command("stats"))
    async def stats(message: Message, rt: BotRuntime, payments: PaymentService) -> None:
        db = payments.db
        today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        text = (
            f"📊 <b>@{rt.username}</b>\n"
            f"Покупателей: {await db.count_users(rt.name)}\n\n"
            f"Сегодня:\n{_revenue_lines(await db.revenue(bot=rt.name, since=today))}\n"
            f"За всё время:\n{_revenue_lines(await db.revenue(bot=rt.name))}"
        )
        if len(payments.registry.all()) > 1:
            text += (
                f"\n\n📊 <b>Все боты ({len(payments.registry.all())})</b>\n"
                f"Сегодня:\n{_revenue_lines(await db.revenue(since=today))}\n"
                f"За всё время:\n{_revenue_lines(await db.revenue())}"
            )
        await message.answer(text)

    @router.message(Command("payments"))
    async def last_payments(message: Message, rt: BotRuntime, payments: PaymentService) -> None:
        items = [
            inv for inv in await payments.db.list_invoices(bot=rt.name, limit=30) if inv.status != PENDING
        ][:10]
        if not items:
            await message.answer("Оплат пока нет.")
            return
        await message.answer("🧾 <b>Последние счета</b>\n\n" + "\n".join(_invoice_line(i) for i in items))

    @router.message(Command("invoice"))
    async def invoice_info(message: Message, command: CommandObject, rt: BotRuntime, payments: PaymentService) -> None:
        inv = await payments.db.get_invoice((command.args or "").strip())
        if inv is None or inv.bot != rt.name:
            await message.answer("Счёт не найден. Формат: /invoice ID")
            return
        lines = [f"<b>{html.escape(k)}</b>: <code>{html.escape(str(v))}</code>" for k, v in inv.to_dict().items() if v not in (None, "")]
        await message.answer("\n".join(lines))

    @router.message(Command("refund"))
    async def refund(message: Message, command: CommandObject, rt: BotRuntime, payments: PaymentService) -> None:
        inv = await payments.db.get_invoice((command.args or "").strip())
        if inv is None or inv.bot != rt.name:
            await message.answer("Счёт не найден. Формат: /refund ID")
            return
        try:
            inv = await payments.refund(inv)
        except PaymentError as exc:
            await message.answer(f"❌ {html.escape(str(exc))}")
            return
        await message.answer(f"↩️ Возврат выполнен: {inv.price}, счёт <code>{inv.id}</code>")

    return router
