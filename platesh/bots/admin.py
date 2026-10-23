"""Команды администратора внутри каждого бота: статистика, платежи, возвраты."""

from __future__ import annotations

import html
from datetime import datetime
from decimal import Decimal

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import Message

from ..access import diagnose
from ..config import Product
from ..delivery import deliver
from ..models import PENDING, Invoice
from ..payments import PaymentError, PaymentService
from ..texts import format_date, format_price
from .registry import BotRuntime

STATUS_ICONS = {"paid": "✅", "pending": "⏳", "expired": "⌛", "refunded": "↩️", "failed": "❌"}

HELP = (
    "<b>Команды администратора</b>\n\n"
    "/stats — выручка и покупатели\n"
    "/payments — последние оплаты\n"
    "/invoice <code>ID</code> — подробности счёта\n"
    "/refund <code>ID</code> — вернуть оплату Stars (доступ закроется)\n\n"
    "/access <code>USER_ID</code> — доступы пользователя\n"
    "/grant <code>USER_ID товар [дней]</code> — выдать доступ без оплаты\n"
    "/revoke <code>USER_ID товар</code> — закрыть доступ и удалить из канала\n"
    "/checkchats — проверить настройки закрытых каналов"
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
            f"Покупателей: {await db.count_users(rt.name)}\n"
            f"Активных доступов: {await db.count_active_accesses(rt.name)}\n\n"
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

    def parse_target(command: CommandObject, rt: BotRuntime, usage: str) -> tuple[int, Product, list[str]] | str:
        args = (command.args or "").split()
        if len(args) < 2 or not args[0].isdigit():
            return f"Формат: {usage}\nТовары: {', '.join(rt.cfg.products) or '—'}"
        product = rt.cfg.products.get(args[1])
        if product is None:
            return f"Товар «{html.escape(args[1])}» не найден. Есть: {', '.join(rt.cfg.products) or '—'}"
        return int(args[0]), product, args[2:]

    @router.message(Command("grant"))
    async def grant(message: Message, command: CommandObject, rt: BotRuntime, payments: PaymentService) -> None:
        target = parse_target(command, rt, "/grant USER_ID товар [дней]")
        if isinstance(target, str):
            await message.answer(target)
            return
        user_id, product, rest = target
        days = None
        if rest:
            if not rest[0].isdigit() or int(rest[0]) < 1:
                await message.answer("Количество дней — целое число больше нуля")
                return
            days = int(rest[0])
        access, renewed = await payments.access.grant(rt, user_id, product, days=days)
        until = f"до {format_date(access.expires_at)}" if access.expires_at else "навсегда"
        answer = f"✅ Доступ «{html.escape(product.title)}» для <code>{user_id}</code> {until}."
        try:
            await deliver(rt, payments.access, user_id, product, access=access, renewed=renewed, tag=f"grant {user_id}")
        except TelegramAPIError as exc:
            answer += (f"\n⚠️ Отправить покупку не удалось ({html.escape(exc.message)}). "
                       f"Пусть пользователь нажмёт /start в боте, и выдай доступ ещё раз.")
        await message.answer(answer)

    @router.message(Command("revoke"))
    async def revoke(message: Message, command: CommandObject, rt: BotRuntime, payments: PaymentService) -> None:
        target = parse_target(command, rt, "/revoke USER_ID товар")
        if isinstance(target, str):
            await message.answer(target)
            return
        user_id, product, _ = target
        if await payments.access.revoke(rt, user_id, product.id):
            await message.answer(f"🚫 Доступ «{html.escape(product.title)}» для <code>{user_id}</code> закрыт.")
        else:
            await message.answer("У пользователя нет активного доступа к этому товару.")

    @router.message(Command("access"))
    async def show_access(message: Message, command: CommandObject, rt: BotRuntime, payments: PaymentService) -> None:
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.answer("Формат: /access USER_ID")
            return
        items = await payments.db.user_accesses(rt.name, int(arg))
        if not items:
            await message.answer("Доступов нет.")
            return
        lines = [f"🔑 Доступы <code>{arg}</code>:"]
        for a in items:
            state = ("✅ навсегда" if a.expires_at is None else f"✅ до {format_date(a.expires_at)}") if a.is_active() \
                else "⛔ закрыт"
            lines.append(f"• {html.escape(a.product_id)} — {state}")
        await message.answer("\n".join(lines))

    @router.message(Command("checkchats"))
    async def check_chats(message: Message, rt: BotRuntime, payments: PaymentService) -> None:
        if not rt.cfg.chats():
            await message.answer("У товаров этого бота нет закрытых каналов (deliver.invite_chat_id).")
            return
        problems = await diagnose(rt)
        if not problems:
            await message.answer("✅ Закрытые каналы настроены правильно.")
            return
        await message.answer("⚠️ <b>Что исправить:</b>\n\n" + "\n".join(f"• {html.escape(p)}" for p in problems))

    return router
