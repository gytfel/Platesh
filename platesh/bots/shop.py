"""Витрина и приём оплат для ботов, которые запускает Platesh (mode: managed)."""

from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
)

from ..config import CRYPTO, STARS, Product
from ..models import EXPIRED, PAID, REFUNDED
from ..payments import PaymentError, PaymentService
from ..texts import asset_label, format_amount, format_date
from .registry import BotRuntime

log = logging.getLogger(__name__)


def price_text(rt: BotRuntime, product: Product) -> str:
    price = rt.cfg.product_price(product)
    return f"{price} за {product.days} дн." if product.days else price


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
        # Цена в фиате (5 USD): по кнопке крипты — выбор монеты.
        data = f"coins:{product.id}" if method == CRYPTO and rt.cfg.crypto_assets() else f"pay:{product.id}:{method}"
        rows.append([InlineKeyboardButton(text=label, callback_data=data)])
    if len(rt.cfg.products) > 1:
        rows.append([InlineKeyboardButton(text=rt.cfg.text("back"), callback_data="cat")])
    text = rt.cfg.text(
        "product",
        title=html.escape(product.title),
        description=product.description,  # описание может содержать HTML-разметку
        price=price_text(rt, product),
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def coins_view(rt: BotRuntime, product: Product) -> tuple[str, InlineKeyboardMarkup]:
    """Выбор монеты для оплаты фиатной цены: CryptoBot пересчитает сумму по курсу."""
    buttons = [InlineKeyboardButton(text=asset_label(asset), callback_data=f"pay:{product.id}:{CRYPTO}:{asset}")
               for asset in rt.cfg.crypto_assets()]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text=rt.cfg.text("back"), callback_data=f"p:{product.id}")])
    text = rt.cfg.text(
        "choose_coin",
        title=html.escape(product.title),
        amount=format_amount(product.price_crypto),
        currency=rt.cfg.crypto.currency,
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def offer_access(rt: BotRuntime, user_chat_id: int, chat_id: int, chat_title: str | None) -> None:
    """Сообщение «вход платный» с кнопками товаров, которые открывают этот чат."""
    rows = [
        [InlineKeyboardButton(text=f"{p.title} · {price_text(rt, p)}", callback_data=f"p:{p.id}")]
        for p in rt.cfg.chat_products(chat_id)
        if rt.cfg.methods_for(p)
    ]
    text = rt.cfg.text("access_required", chat=html.escape(chat_title or "Канал"))
    try:
        await rt.bot.send_message(user_chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    except TelegramAPIError:
        pass  # человек не запускал бота — написать ему нельзя


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

    @router.callback_query(F.data.startswith("coins:"))
    async def show_coins(callback: CallbackQuery, rt: BotRuntime) -> None:
        product = rt.cfg.products.get(callback.data[6:])
        if product is None or CRYPTO not in rt.cfg.methods_for(product) or not rt.cfg.crypto_assets():
            await callback.answer(rt.cfg.text("method_unavailable"), show_alert=True)
            return
        await show(callback, *coins_view(rt, product))
        await callback.answer()

    @router.callback_query(F.data.startswith("pay:"))
    async def pay(callback: CallbackQuery, rt: BotRuntime, payments: PaymentService) -> None:
        _, product_id, method, asset = (callback.data.split(":", 3) + ["", "", ""])[:4]
        product = rt.cfg.products.get(product_id)
        if product is None or method not in (STARS, CRYPTO):
            await callback.answer(rt.cfg.text("method_unavailable"), show_alert=True)
            return
        await payments.remember_user(rt, callback.from_user)
        chat_id = callback.message.chat.id if callback.message is not None else callback.from_user.id
        try:
            inv = await payments.create_invoice(
                rt, method=method, user_id=callback.from_user.id, product=product, stars_link=False, reuse=True,
                asset=asset or None,
            )
            await payments.send_invoice(rt, inv, chat_id, asset=asset or None)
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
        user_id = message.from_user.id
        accesses = await payments.access.active_accesses(rt, user_id)
        items = await payments.db.list_invoices(bot=rt.name, user_id=user_id, status=PAID, limit=20)
        if not items and not accesses:
            await message.answer(rt.cfg.text("purchases_empty"))
            return
        lines = []
        if accesses:
            lines += [rt.cfg.text("my_access_title"), ""]
            for a in accesses:
                product = rt.cfg.products.get(a.product_id)
                title = html.escape(product.title if product else a.product_id)
                until = rt.cfg.text("access_date", date=format_date(a.expires_at)) if a.expires_at else rt.cfg.text("access_forever")
                lines.append(f"• {title} — {until}")
            lines.append("")
        if items:
            lines += [rt.cfg.text("purchases_title"), ""]
            for inv in items:
                date = datetime.fromtimestamp(inv.paid_at or inv.created_at).strftime("%d.%m.%Y")
                lines.append(f"• {html.escape(inv.title)} — {inv.price}, {date}")
        await message.answer("\n".join(lines).strip())

    @router.chat_join_request()
    async def join_request(request: ChatJoinRequest, rt: BotRuntime, payments: PaymentService) -> None:
        """Заявка на вход в закрытый чат: одобряем только тем, у кого оплачен доступ."""
        chat_id = request.chat.id
        if chat_id not in rt.cfg.chats():
            return
        try:
            if await payments.access.has_chat_access(rt, request.from_user.id, chat_id):
                await request.approve()
                return
            if not rt.cfg.guard:
                return  # охрана выключена — заявку рассматривает владелец вручную
            await request.decline()
        except TelegramBadRequest as exc:
            log.info("Заявка %s в %s уже обработана: %s", request.from_user.id, chat_id, exc.message)
            return
        log.info("Бот «%s»: заявка %s в чат %s отклонена — нет оплаты", rt.name, request.from_user.id, chat_id)
        await offer_access(rt, request.user_chat_id, chat_id, request.chat.title)

    @router.my_chat_member()
    async def bot_added_to_chat(event: ChatMemberUpdated, rt: BotRuntime, payments: PaymentService) -> None:
        """Подсказка при настройке: бота добавили в канал/группу — присылаем админам ID для config.yaml."""
        status = event.new_chat_member.status
        if event.chat.type == "private" or status not in ("administrator", "member"):
            return
        if event.old_chat_member.status == status:
            return
        role = "администратором" if status == "administrator" else "участником (нужно сделать администратором!)"
        title = html.escape(event.chat.title or str(event.chat.id))
        await payments.notifier.alert(
            rt,
            f"ℹ️ Бот добавлен в «{title}» {role}.\n"
            f"ID для config.yaml (deliver.invite_chat_id): <code>{event.chat.id}</code>",
        )

    @router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
    async def member_joined(event: ChatMemberUpdated, rt: BotRuntime, payments: PaymentService) -> None:
        """Охрана: кто попал в закрытый чат в обход оплаты (старая или публичная ссылка), удаляется."""
        chat_id = event.chat.id
        member = event.new_chat_member
        user = member.user
        if not rt.cfg.guard or chat_id not in rt.cfg.chats():
            return
        if user.is_bot or member.status in ("administrator", "creator"):
            return
        # Добавил сам бот (одобрил заявку) или админ бота вручную — пропускаем.
        if event.from_user.id != user.id and (event.from_user.id == rt.id or rt.is_admin(event.from_user.id)):
            return
        if await payments.access.has_chat_access(rt, user.id, chat_id):
            return
        try:
            await payments.access.kick(rt, chat_id, user.id)
        except TelegramAPIError as exc:
            log.warning("Бот «%s»: не удалось удалить %s из %s: %s", rt.name, user.id, chat_id, exc)
            await payments.notifier.alert(
                rt,
                f"⚠️ В «{html.escape(event.chat.title or str(chat_id))}» вошёл <code>{user.id}</code> без оплаты, "
                f"но удалить его не удалось: <code>{html.escape(exc.message)}</code>",
            )
            return
        log.info("Бот «%s»: %s вошёл в %s без оплаты и удалён", rt.name, user.id, chat_id)
        await offer_access(rt, user.id, chat_id, event.chat.title)

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

