"""Ядро: создание счетов Stars/CryptoBot, приём оплат, выдача покупок, возвраты."""

from __future__ import annotations

import json
import logging
import secrets
import time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Callable

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, User

from .config import CRYPTO, FIAT_CURRENCIES, STARS, Config, Product
from .cryptopay import CryptoPayClient, CryptoPayError, invoice_url, verify_signature
from .db import Database
from .delivery import deliver
from .models import EXPIRED, FAILED, PAID, PENDING, REFUNDED, Invoice, parse_payload
from .notifier import Notifier
from .texts import format_amount

if TYPE_CHECKING:
    from .bots.registry import BotRegistry, BotRuntime

log = logging.getLogger(__name__)

STARS_CURRENCY = "XTR"
STARS_REUSE_WINDOW = 3600
# Не переиспользуем крипто-счёт, если до его истечения осталось меньше 5 минут.
CRYPTO_REUSE_MARGIN = 300
KNOWN_USERS_LIMIT = 100_000


class PaymentError(Exception):
    """Ошибка, текст которой можно показать покупателю или клиенту API."""


def new_invoice_id() -> str:
    return secrets.token_hex(6)


class PaymentService:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        registry: BotRegistry,
        notifier: Notifier,
        *,
        crypto_factory: Callable[..., CryptoPayClient] = CryptoPayClient,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.registry = registry
        self.notifier = notifier
        self._crypto_factory = crypto_factory
        self._crypto: dict[tuple[str, bool], CryptoPayClient] = {}
        self._known_users: dict[tuple[str, int], tuple[str | None, str | None]] = {}

    # ------------------------------------------------------------------ crypto clients

    def crypto(self, rt: BotRuntime) -> CryptoPayClient:
        c = rt.cfg.crypto
        if not c.token:
            raise PaymentError(rt.cfg.text("method_unavailable"))
        key = (c.token, c.testnet)
        if key not in self._crypto:
            self._crypto[key] = self._crypto_factory(c.token, testnet=c.testnet)
        return self._crypto[key]

    def crypto_clients(self) -> list[CryptoPayClient]:
        for rt in self.registry.all():
            if rt.cfg.crypto.enabled:
                self.crypto(rt)
        return list(self._crypto.values())

    async def close(self) -> None:
        for client in self._crypto.values():
            await client.close()

    # ------------------------------------------------------------------ users

    async def remember_user(self, rt: BotRuntime, user: User | None) -> None:
        if user is None or user.is_bot:
            return
        key, value = (rt.name, user.id), (user.username, user.first_name)
        if self._known_users.get(key) == value:
            return
        await self.db.upsert_user(rt.name, user.id, user.username, user.first_name)
        if len(self._known_users) >= KNOWN_USERS_LIMIT:
            self._known_users.clear()
        self._known_users[key] = value

    # ------------------------------------------------------------------ invoices

    async def create_invoice(
        self,
        rt: BotRuntime,
        *,
        method: str,
        user_id: int | None = None,
        product: Product | None = None,
        amount: Decimal | int | str | None = None,
        currency: str | None = None,
        title: str | None = None,
        description: str | None = None,
        payload: str | None = None,
        stars_link: bool = True,
        reuse: bool = False,
    ) -> Invoice:
        """Создаёт счёт. Для товара цена берётся из конфига, иначе нужны amount и title."""
        cfg = rt.cfg
        if not cfg.method_enabled(method):
            raise PaymentError(cfg.text("method_unavailable"))

        if product is not None:
            if method not in cfg.methods_for(product):
                raise PaymentError(cfg.text("method_unavailable"))
            amount = product.price_stars if method == STARS else product.price_crypto
            title, description = product.title, product.description
        elif amount is None or not title:
            raise PaymentError("Укажи product_id или amount и title")

        try:
            value = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            raise PaymentError("amount должен быть числом") from None
        if not value.is_finite() or value <= 0:
            raise PaymentError("amount должен быть больше нуля")
        if method == STARS:
            if value != value.to_integral_value():
                raise PaymentError("Сумма в Stars должна быть целым числом")
            currency = STARS_CURRENCY
        else:
            currency = (currency or cfg.crypto.currency).upper()

        if reuse and product is not None and user_id is not None:
            window = STARS_REUSE_WINDOW if method == STARS else cfg.crypto.expires_in - CRYPTO_REUSE_MARGIN
            existing = await self.db.find_reusable(rt.name, user_id, product.id, method, int(time.time()) - window)
            if existing is not None and (method == STARS or existing.pay_url):
                return existing

        inv = Invoice(
            id=new_invoice_id(),
            bot=rt.name,
            method=method,
            amount=value,
            currency=currency,
            title=title,
            description=description or "",
            user_id=user_id,
            product_id=product.id if product else None,
            payload=payload,
            created_at=int(time.time()),
        )
        await self.db.add_invoice(inv)
        try:
            if method == STARS:
                if stars_link:
                    inv.pay_url = await rt.bot.create_invoice_link(**self._stars_invoice_args(inv))
            else:
                c = cfg.crypto
                data = await self.crypto(rt).create_invoice(
                    value,
                    currency=currency,
                    is_fiat=currency in FIAT_CURRENCIES,
                    accepted_assets=c.accepted_assets,
                    description=title,
                    payload=inv.tg_payload,
                    expires_in=c.expires_in,
                    paid_btn_url=rt.link,
                )
                inv.external_id = str(data["invoice_id"])
                inv.pay_url = invoice_url(data)
        except (TelegramAPIError, CryptoPayError, OSError, KeyError) as exc:
            log.error("Бот «%s»: не удалось создать счёт %s (%s): %s", rt.name, inv.id, method, exc)
            await self.db.set_status(inv.id, FAILED, expected=PENDING)
            raise PaymentError(cfg.text("payment_error")) from exc
        if inv.pay_url or inv.external_id:
            await self.db.set_payment_link(inv.id, inv.pay_url, inv.external_id)
        log.info("Бот «%s»: создан счёт %s — %s, %s", rt.name, inv.id, inv.price, inv.title)
        return inv

    @staticmethod
    def _stars_invoice_args(inv: Invoice) -> dict[str, Any]:
        # Ограничения Telegram: title 1–32 символа, description 1–255.
        title = inv.title[:32]
        return {
            "title": title,
            "description": (inv.description or inv.title)[:255],
            "payload": inv.tg_payload,
            "currency": STARS_CURRENCY,
            "prices": [LabeledPrice(label=title, amount=int(inv.amount))],
        }

    async def send_invoice(self, rt: BotRuntime, inv: Invoice, chat_id: int) -> None:
        """Отправляет счёт в чат: нативный счёт Stars или сообщение с кнопкой оплаты CryptoBot."""
        if inv.method == STARS:
            await rt.bot.send_invoice(chat_id, **self._stars_invoice_args(inv))
            return
        rows = [[InlineKeyboardButton(text=rt.cfg.text("crypto_pay_button"), url=inv.pay_url)]]
        if rt.cfg.managed:
            # Кнопку «проверить» обрабатывает Platesh, поэтому только у ботов, которые он запускает.
            rows.append([InlineKeyboardButton(text=rt.cfg.text("check_button"), callback_data=f"chk:{inv.id}")])
        text = rt.cfg.text(
            "crypto_invoice",
            amount=format_amount(inv.amount),
            currency=inv.currency,
            minutes=rt.cfg.crypto.expires_in // 60,
        )
        await rt.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    # ------------------------------------------------------------------ paid handling

    async def _on_paid(self, rt: BotRuntime, inv: Invoice) -> None:
        log.info("Бот «%s»: счёт %s оплачен (%s)", rt.name, inv.id, inv.price)
        product = rt.cfg.products.get(inv.product_id) if inv.product_id else None
        try:
            await deliver(rt, inv, product)
        except Exception as exc:  # выдача не должна ломать приём платежа
            log.exception("Бот «%s»: не удалось выдать покупку по счёту %s", rt.name, inv.id)
            await self.notifier.alert(
                rt,
                f"⚠️ Счёт <code>{inv.id}</code> оплачен, но выдать покупку не удалось: "
                f"<code>{type(exc).__name__}: {exc}</code>\nВыдай вручную покупателю <code>{inv.user_id}</code>.",
            )
        self.notifier.payment_received(rt, inv)

    # ------------------------------------------------------------------ Telegram Stars

    async def validate_pre_checkout(self, rt: BotRuntime, payload: str, currency: str, total_amount: int) -> str | None:
        """None — можно принимать оплату, иначе текст ошибки для покупателя."""
        inv_id = parse_payload(payload)
        inv = await self.db.get_invoice(inv_id) if inv_id else None
        if (
            inv is None
            or inv.bot != rt.name
            or inv.method != STARS
            or inv.status != PENDING
            or currency != STARS_CURRENCY
            or total_amount != int(inv.amount)
        ):
            return rt.cfg.text("precheckout_error")
        return None

    async def process_stars_payment(
        self,
        rt: BotRuntime,
        *,
        payload: str,
        charge_id: str,
        total_amount: int,
        currency: str,
        user_id: int,
    ) -> Invoice | None:
        inv_id = parse_payload(payload)
        inv = await self.db.get_invoice(inv_id) if inv_id else None
        if inv is None or inv.bot != rt.name or currency != STARS_CURRENCY or total_amount != int(inv.amount):
            await self._refund_orphan(rt, user_id, charge_id, total_amount, "оплата не соответствует ни одному счёту")
            return None
        paid = await self.db.mark_paid(inv.id, charge_id=charge_id, user_id=user_id)
        inv = await self.db.get_invoice(inv.id)
        if paid:
            await self._on_paid(rt, inv)
        elif inv.charge_id != charge_id:
            await self._refund_orphan(rt, user_id, charge_id, total_amount, f"повторная оплата счёта {inv.id}")
        return inv

    async def _refund_orphan(self, rt: BotRuntime, user_id: int, charge_id: str, amount: int, reason: str) -> None:
        log.warning("Бот «%s»: возврат %s ⭐ пользователю %s — %s", rt.name, amount, user_id, reason)
        try:
            await rt.bot.refund_star_payment(user_id, charge_id)
            result = "деньги автоматически возвращены"
        except TelegramAPIError as exc:
            result = f"автовозврат не удался: {exc}"
        await self.notifier.alert(
            rt,
            f"⚠️ Получено {amount} ⭐ от <code>{user_id}</code>: {reason}; {result}.\n"
            f"charge_id: <code>{charge_id}</code>",
        )

    # ------------------------------------------------------------------ CryptoBot

    async def _apply_crypto_status(self, rt: BotRuntime, inv: Invoice, item: dict[str, Any]) -> None:
        status = item.get("status")
        if status == "paid":
            charge_id = item.get("hash") or str(item.get("invoice_id"))
            paid = await self.db.mark_paid(
                inv.id,
                charge_id=charge_id,
                paid_asset=item.get("paid_asset"),
                paid_amount=item.get("paid_amount"),
            )
            if paid:
                await self._on_paid(rt, await self.db.get_invoice(inv.id))
        elif status == "expired":
            await self.db.set_status(inv.id, EXPIRED, expected=PENDING)

    async def refresh(self, inv: Invoice) -> Invoice:
        """Сразу проверяет статус крипто-счёта в CryptoBot (кнопка «Проверить оплату»)."""
        rt = self.registry.get(inv.bot)
        if rt is None or inv.method != CRYPTO or inv.status != PENDING or not inv.external_id:
            return inv
        try:
            items = await self.crypto(rt).get_invoices([inv.external_id])
        except (CryptoPayError, OSError) as exc:
            log.warning("Не удалось проверить счёт %s: %s", inv.id, exc)
            return inv
        for item in items:
            if str(item.get("invoice_id")) == inv.external_id:
                await self._apply_crypto_status(rt, inv, item)
        return await self.db.get_invoice(inv.id) or inv

    async def poll_crypto(self) -> None:
        """Опрашивает CryptoBot по всем ожидающим счетам (работает и без вебхука)."""
        groups: dict[int, tuple[CryptoPayClient, list[tuple[BotRuntime, Invoice]]]] = {}
        for inv in await self.db.pending_crypto():
            rt = self.registry.get(inv.bot)
            if rt is None or not rt.cfg.crypto.enabled:
                continue
            client = self.crypto(rt)
            groups.setdefault(id(client), (client, []))[1].append((rt, inv))
        for client, entries in groups.values():
            try:
                items = await client.get_invoices([inv.external_id for _, inv in entries])
            except (CryptoPayError, OSError) as exc:
                log.warning("Опрос CryptoBot не удался: %s", exc)
                continue
            by_id = {str(item.get("invoice_id")): item for item in items}
            for rt, inv in entries:
                item = by_id.get(inv.external_id)
                if item is not None:
                    await self._apply_crypto_status(rt, inv, item)

    async def handle_crypto_webhook(self, body: bytes, signature: str | None) -> bool:
        """Обработка вебхука CryptoBot. False — подпись не подошла ни к одному токену."""
        signer = next((c for c in self.crypto_clients() if verify_signature(c.token, body, signature)), None)
        if signer is None:
            return False
        try:
            update = json.loads(body)
        except ValueError:
            return True
        if update.get("update_type") != "invoice_paid":
            return True
        item = update.get("payload") or {}
        external_id = str(item.get("invoice_id"))
        inv_id = parse_payload(item.get("payload"))
        inv = await self.db.get_invoice(inv_id) if inv_id else None
        if inv is None:
            inv = await self.db.find_by_external(CRYPTO, external_id)
        if inv is None or inv.method != CRYPTO or inv.external_id != external_id:
            log.warning("Вебхук CryptoBot: неизвестный счёт %s", external_id)
            return True
        rt = self.registry.get(inv.bot)
        if rt is None or not rt.cfg.crypto.enabled or self.crypto(rt) is not signer:
            log.warning("Вебхук CryptoBot: счёт %s подписан чужим токеном", inv.id)
            return True
        await self._apply_crypto_status(rt, inv, item)
        return True

    # ------------------------------------------------------------------ refunds

    async def refund(self, inv: Invoice) -> Invoice:
        if inv.status != PAID:
            raise PaymentError("Вернуть можно только оплаченный счёт")
        if inv.method != STARS:
            raise PaymentError("Автовозврат доступен только для Stars. Крипту верни вручную через @CryptoBot")
        rt = self.registry.get(inv.bot)
        if rt is None:
            raise PaymentError(f"Бот «{inv.bot}» не подключён")
        try:
            await rt.bot.refund_star_payment(inv.user_id, inv.charge_id)
        except TelegramAPIError as exc:
            raise PaymentError(f"Telegram отклонил возврат: {exc.message}") from exc
        await self.db.set_status(inv.id, REFUNDED, expected=PAID)
        inv = await self.db.get_invoice(inv.id)
        try:
            await rt.bot.send_message(inv.user_id, rt.cfg.text("refunded", title=inv.title, amount=inv.price))
        except TelegramAPIError:
            pass
        self.notifier.refunded(rt, inv)
        return inv
