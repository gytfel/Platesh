"""Витрина и приём оплат для ботов, которые запускает Platesh (mode: managed)."""

from __future__ import annotations

import html
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
)

from ..config import CRYPTO, STARS, Product
from ..models import EXPIRED, PAID, REFUNDED
from ..payments import PaymentError, PaymentService
from ..texts import format_amount
from .registry import BotRuntime


def catalog_view(rt: BotRuntime, user_id: int | None) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = [
        [InlineKeyboardButton(text=f"{p.title} · {rt.cfg.product_price(p)}", callback_data=f"p:{p.id}")]
        for p in rt.cfg.products.values()
        if rt.cfg.methods_for(p)
    ]
    text = rt.cfg.text("welcome") if rows else rt.cfg.text("no_products")
    if rt.is_admin(user_id):
        text += rt.cfg.text("admin_hint")
    return text, InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def product_view(rt: BotRuntime, product: Product) -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    for method in rt.cfg.methods_for(product):
        if method == STARS:
            label = rt.cfg.text("pay_stars", amount=product.price_stars)
        else:
            label = rt.cfg.text(
                "pay_crypto", amount=format_amount(product.price_crypto), currency=rt.cfg.crypto.currency
            )
        rows.append([InlineKeyboardButton(text=label, callback_data=f"pay:{product.id}:{method}")])
    if len(rt.cfg.products) > 1:
        rows.append([InlineKeyboardButton(text=rt.cfg.text("back"), callback_data="cat")])
    text = rt.cfg.text(
        "product",
        title=html.escape(product.title),
        description=product.description,  # описание может содержать HTML-разметку
        price=rt.cfg.product_price(product),
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def show(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Заменяет сообщение с кнопками; если это невозможно — отправляет новое."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "not modified" in exc.message:
                return
    await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)


def bot_commands(rt: BotRuntime) -> list[BotCommand]:
    commands = [
        BotCommand(command="start", description="Каталог"),
        BotCommand(command="purchases", description="Мои покупки"),
        BotCommand(command="paysupport", description="Помощь с оплатой"),
    ]
    if rt.cfg.terms:
        commands.append(BotCommand(command="terms", description="Условия"))
    return commands


def create_shop_router() -> Router:
    router = Router(name="shop")

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject, rt: BotRuntime, payments: PaymentService) -> None:
        await payments.remember_user(rt, message.from_user)
        arg = command.args or ""
        product = rt.cfg.products.get(arg[2:]) if arg.startswith("p_") else None
        if product is not None and rt.cfg.methods_for(product):
            text, markup = product_view(rt, product)
        else:
            text, markup = catalog_view(rt, message.from_user.id if message.from_user else None)
        await message.answer(text, reply_markup=markup)

    @router.callback_query(F.data == "cat")
    async def show_catalog(callback: CallbackQuery, rt: BotRuntime) -> None:
        await show(callback, *catalog_view(rt, callback.from_user.id))
        await callback.answer()

    @router.callback_query(F.data.startswith("p:"))
    async def show_product(callback: CallbackQuery, rt: BotRuntime) -> None:
        product = rt.cfg.products.get(callback.data[2:])
        if product is None or not rt.cfg.methods_for(product):
            await callback.answer(rt.cfg.text("method_unavailable"), show_alert=True)
            return
        await show(callback, *product_view(rt, product))
        await callback.answer()

    @router.callback_query(F.data.startswith("pay:"))
    async def pay(callback: CallbackQuery, rt: BotRuntime, payments: PaymentService) -> None:
        _, product_id, method = (callback.data.split(":", 2) + ["", ""])[:3]
        product = rt.cfg.products.get(product_id)
        if product is None or method not in (STARS, CRYPTO):
            await callback.answer(rt.cfg.text("method_unavailable"), show_alert=True)
            return
        await payments.remember_user(rt, callback.from_user)
        chat_id = callback.message.chat.id if callback.message is not None else callback.from_user.id
        try:
            inv = await payments.create_invoice(
                rt, method=method, user_id=callback.from_user.id, product=product, stars_link=False, reuse=True
            )
            await payments.send_invoice(rt, inv, chat_id)
        except PaymentError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("chk:"))
    async def check(callback: CallbackQuery, rt: BotRuntime, payments: PaymentService) -> None:
        inv = await payments.db.get_invoice(callback.data[4:])
        if inv is None or inv.bot != rt.name or inv.user_id != callback.from_user.id:
            await callback.answer(rt.cfg.text("precheckout_error"), show_alert=True)
            return
        inv = await payments.refresh(inv)
        if inv.status in (PAID, REFUNDED):
            await callback.answer(rt.cfg.text("already_paid"), show_alert=True)
        elif inv.status == EXPIRED:
            await callback.answer(rt.cfg.text("invoice_expired"), show_alert=True)
        else:
            await callback.answer(rt.cfg.text("not_paid_yet"), show_alert=True)

    @router.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery, rt: BotRuntime, payments: PaymentService) -> None:
        error = await payments.validate_pre_checkout(rt, query.invoice_payload, query.currency, query.total_amount)
        await query.answer(ok=error is None, error_message=error)

    @router.message(F.successful_payment)
    async def successful_payment(message: Message, rt: BotRuntime, payments: PaymentService) -> None:
        await payments.remember_user(rt, message.from_user)
        sp = message.successful_payment
        await payments.process_stars_payment(
            rt,
            payload=sp.invoice_payload,
            charge_id=sp.telegram_payment_charge_id,
            total_amount=sp.total_amount,
            currency=sp.currency,
            user_id=message.from_user.id,
        )

    @router.message(Command("purchases"))
    async def purchases(message: Message, rt: BotRuntime, payments: PaymentService) -> None:
        items = await payments.db.list_invoices(bot=rt.name, user_id=message.from_user.id, status=PAID, limit=20)
        if not items:
            await message.answer(rt.cfg.text("purchases_empty"))
            return
        lines = [rt.cfg.text("purchases_title"), ""]
        for inv in items:
            date = datetime.fromtimestamp(inv.paid_at or inv.created_at).strftime("%d.%m.%Y")
            lines.append(f"• {html.escape(inv.title)} — {inv.price}, {date}")
        await message.answer("\n".join(lines))

    @router.message(Command("paysupport"))
    async def paysupport(message: Message, rt: BotRuntime) -> None:
        await message.answer(rt.cfg.text("support"))

    @router.message(Command("terms"))
    async def terms(message: Message, rt: BotRuntime) -> None:
        await message.answer(rt.cfg.terms or rt.cfg.text("support"))

    @router.message(F.chat.type == "private", F.text)
    async def fallback(message: Message, rt: BotRuntime) -> None:
        text, markup = catalog_view(rt, message.from_user.id if message.from_user else None)
        await message.answer(text, reply_markup=markup)

    return router

