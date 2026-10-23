"""Клиент Platesh для ботов со своим кодом (mode: external).

Скопируй этот файл в проект своего бота. Нужны aiohttp и aiogram 3.

    from platesh_client import PlateshClient

    # Если бота запускает Platesh (run: в config.yaml) — адрес, ключ и админы придут сами:
    platesh = PlateshClient.from_env()
    # Если запускаешь бота сам — укажи их явно:
    # platesh = PlateshClient("http://127.0.0.1:8080", api_key="ключ этого бота", bot="my_own_bot")

    # 1) Обязательно: подтверждение оплат Stars, кнопки «Оплатить/Продлить», охрана закрытых каналов.
    dp.include_router(platesh.router())

    # 2) Пейволл: без оплаченного доступа бот не отвечает, а предлагает оплатить.
    platesh.install_paywall(dp, products=["premium"])

    # 3) Или проверяй доступ сам там, где нужно:
    if await platesh.has_access(user_id, product_id="premium"):
        ...

    # Счёт вручную: Platesh сам пришлёт его пользователю
    await platesh.create_invoice(user_id=123, method="stars", product_id="premium", send=True)

Уведомления об оплатах придут на notify_url (см. README), проверить подпись — verify_webhook().
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable, Iterable

import aiohttp

log = logging.getLogger("platesh_client")

PAYLOAD_PREFIX = "pl:"
CALLBACK_PREFIX = "platesh:"
ACCESS_TTL = 60  # сколько секунд помнить «доступ есть»
NO_ACCESS_TTL = 5  # и «доступа нет» (коротко, чтобы оплата криптой подхватывалась быстро)

PAYWALL_TEXT = "🔒 Этот бот доступен после оплаты. Выбери вариант:"
UNAVAILABLE_TEXT = "Сервис оплаты временно недоступен, попробуй через минуту."


class PlateshError(Exception):
    pass


class PlateshClient:
    def __init__(self, base_url: str, api_key: str, bot: str, *, admins: Iterable[int] = (), timeout: float = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.bot = bot
        self.admins = {int(a) for a in admins}  # им пейволл не мешает
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._access_cache: dict[tuple[int, str | None, int | None], tuple[bool, float]] = {}
        self._bot_info: dict[str, Any] | None = None

    @classmethod
    def from_env(cls, **kwargs: Any) -> PlateshClient:
        """Настройки из переменных, которые Platesh передаёт боту при запуске (run: в config.yaml)."""
        url, key, bot = os.environ.get("PLATESH_URL"), os.environ.get("PLATESH_KEY"), os.environ.get("PLATESH_BOT")
        if not (url and key and bot):
            raise PlateshError(
                "Не заданы PLATESH_URL, PLATESH_KEY и PLATESH_BOT. Запускай бота через Platesh "
                "(run: в config.yaml) или создай клиент явно: PlateshClient(url, api_key=..., bot=...)"
            )
        admins = [int(a) for a in os.environ.get("PLATESH_ADMINS", "").split(",") if a.strip()]
        return cls(url, key, bot, admins=admins, **kwargs)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers, timeout=self._timeout)
        async with self._session.request(method, f"{self.base_url}{path}", **kwargs) as resp:
            data = await resp.json(content_type=None)
        if not data.get("ok"):
            raise PlateshError(data.get("error", f"HTTP {resp.status}"))
        return data["result"]

    # ------------------------------------------------------------------ счета

    async def create_invoice(
        self,
        *,
        method: str,
        user_id: int | None = None,
        product_id: str | None = None,
        amount: str | int | float | None = None,
        title: str | None = None,
        description: str | None = None,
        currency: str | None = None,
        payload: str | None = None,
        send: bool = False,
    ) -> dict[str, Any]:
        body = {
            "bot": self.bot,
            "method": method,
            "user_id": user_id,
            "product_id": product_id,
            "amount": str(amount) if amount is not None else None,
            "title": title,
            "description": description,
            "currency": currency,
            "payload": payload,
            "send": send,
        }
        return await self._request("POST", "/api/v1/invoices", json={k: v for k, v in body.items() if v is not None})

    async def get_invoice(self, invoice_id: str, *, refresh: bool = False) -> dict[str, Any]:
        params = {"refresh": "1"} if refresh else None
        return await self._request("GET", f"/api/v1/invoices/{invoice_id}", params=params)

    async def bot_info(self) -> dict[str, Any]:
        """Настройки бота из config.yaml: способы оплаты, товары, цены, закрытые чаты."""
        if self._bot_info is None:
            bots = await self._request("GET", "/api/v1/bots")
            self._bot_info = next((b for b in bots if b["name"] == self.bot), None)
            if self._bot_info is None:
                raise PlateshError(f"бот «{self.bot}» не найден в Platesh")
        return self._bot_info

    # ------------------------------------------------------------------ доступы

    def forget(self, user_id: int) -> None:
        """Сбросить кэш доступа пользователя (например, сразу после оплаты)."""
        for key in [k for k in self._access_cache if k[0] == user_id]:
            del self._access_cache[key]

    async def has_access(self, user_id: int, *, product_id: str | None = None, chat_id: int | None = None) -> bool:
        """Есть ли у пользователя оплаченный и не истёкший доступ (к товару, к чату или любой)."""
        key = (user_id, product_id, chat_id)
        cached = self._access_cache.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        params: dict[str, str] = {"bot": self.bot, "user_id": str(user_id)}
        if product_id:
            params["product_id"] = product_id
        if chat_id is not None:
            params["chat_id"] = str(chat_id)
        try:
            active = bool((await self._request("GET", "/api/v1/access", params=params))["active"])
        except (PlateshError, aiohttp.ClientError, asyncio.TimeoutError):
            if cached and cached[0]:
                return True  # Platesh недоступен — не выгоняем того, кто только что был с доступом
            raise
        self._access_cache[key] = (active, time.monotonic() + (ACCESS_TTL if active else NO_ACCESS_TTL))
        return active

    async def has_any_access(self, user_id: int, products: Iterable[str] | None) -> bool:
        if not products:
            return await self.has_access(user_id)
        for product_id in products:
            if await self.has_access(user_id, product_id=product_id):
                return True
        return False

    # ------------------------------------------------------------------ Telegram Stars

    async def pre_checkout(self, payload: str, currency: str, total_amount: int) -> str | None:
        """None — платёж можно принять, иначе текст ошибки для покупателя."""
        result = await self._request("POST", "/api/v1/stars/pre-checkout", json={
            "bot": self.bot, "payload": payload, "currency": currency, "total_amount": total_amount,
        })
        return None if result["accept"] else result["error"]

    async def stars_paid(self, *, payload: str, charge_id: str, total_amount: int, currency: str,
                         user_id: int, username: str | None = None, first_name: str | None = None) -> Any:
        return await self._request("POST", "/api/v1/stars/paid", json={
            "bot": self.bot, "payload": payload, "charge_id": charge_id, "total_amount": total_amount,
            "currency": currency, "user_id": user_id, "username": username, "first_name": first_name,
        })

    # ------------------------------------------------------------------ кнопки оплаты

    async def offer_markup(self, products: Iterable[str] | None = None):
        """Кнопки «⭐ Товар — 100 Stars» / «💎 Товар — 5 USD» для выбранных (или всех) товаров."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        info = await self.bot_info()
        wanted = set(products) if products else None
        rows = []
        for p in info["products"]:
            if wanted is not None and p["id"] not in wanted:
                continue
            period = f" / {p['days']} дн." if p.get("days") else ""
            if p.get("price_stars") is not None and "stars" in info["payments"]:
                rows.append([InlineKeyboardButton(
                    text=f"⭐ {p['title']} — {p['price_stars']} Stars{period}",
                    callback_data=f"{CALLBACK_PREFIX}pay:{p['id']}:stars")])
            if p.get("price_crypto") is not None and "crypto" in info["payments"]:
                rows.append([InlineKeyboardButton(
                    text=f"💎 {p['title']} — {p['price_crypto']} {info['crypto_currency']}{period}",
                    callback_data=f"{CALLBACK_PREFIX}pay:{p['id']}:crypto")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # ------------------------------------------------------------------ aiogram

    def router(self, *, guard: bool = True):
        """Роутер aiogram 3. Подключи его раньше своих роутеров.

        - подтверждает и учитывает оплаты Stars по счетам Platesh (чужие счета не трогает);
        - обрабатывает кнопки «Оплатить» и «Продлить» (в т.ч. из напоминаний Platesh);
        - guard=True: в закрытые чаты товаров пускает только тех, у кого оплачен доступ
          (заявки на вход одобряет/отклоняет, вошедших в обход оплаты удаляет).
        """
        from aiogram import Bot, F, Router
        from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
        from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
        from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, Message, PreCheckoutQuery

        router = Router(name="platesh")

        @router.pre_checkout_query(F.invoice_payload.startswith(PAYLOAD_PREFIX))
        async def on_pre_checkout(query: PreCheckoutQuery) -> None:
            try:
                error = await self.pre_checkout(query.invoice_payload, query.currency, query.total_amount)
            except (PlateshError, aiohttp.ClientError, asyncio.TimeoutError):
                error = UNAVAILABLE_TEXT
            await query.answer(ok=error is None, error_message=error)

        @router.message(F.successful_payment.invoice_payload.startswith(PAYLOAD_PREFIX))
        async def on_paid(message: Message) -> None:
            sp = message.successful_payment
            # Деньги уже списаны — стараемся донести событие до Platesh, даже если он моргнул.
            for delay in (0, 1, 3, 10, 30):
                await asyncio.sleep(delay)
                try:
                    await self.stars_paid(
                        payload=sp.invoice_payload,
                        charge_id=sp.telegram_payment_charge_id,
                        total_amount=sp.total_amount,
                        currency=sp.currency,
                        user_id=message.from_user.id,
                        username=message.from_user.username,
                        first_name=message.from_user.first_name,
                    )
                    self.forget(message.from_user.id)
                    return
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue
            raise PlateshError(f"Platesh недоступен, оплата {sp.telegram_payment_charge_id} не учтена")

        @router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}buy:"))
        async def on_buy(callback: CallbackQuery) -> None:
            product_id = callback.data.split(":", 2)[2]
            await callback.answer()
            await callback.bot.send_message(
                callback.from_user.id, PAYWALL_TEXT, reply_markup=await self.offer_markup([product_id])
            )

        @router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}pay:"))
        async def on_pay(callback: CallbackQuery) -> None:
            _, _, product_id, method = (callback.data.split(":", 3) + ["", ""])[:4]
            try:
                await self.create_invoice(method=method, product_id=product_id, user_id=callback.from_user.id, send=True)
            except (PlateshError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Не удалось создать счёт: %s", exc)
                await callback.answer(UNAVAILABLE_TEXT, show_alert=True)
                return
            await callback.answer()

        if not guard:
            return router

        async def product_chats() -> set[int]:
            info = await self.bot_info()
            return {p["chat_id"] for p in info["products"] if p.get("chat_id") is not None}

        @router.chat_join_request()
        async def on_join_request(request: ChatJoinRequest) -> None:
            if request.chat.id not in await product_chats():
                return
            try:
                if await self.has_access(request.from_user.id, chat_id=request.chat.id):
                    await request.approve()
                    return
                await request.decline()
            except TelegramBadRequest:
                return
            with suppress(TelegramAPIError):
                await request.bot.send_message(request.user_chat_id, PAYWALL_TEXT, reply_markup=await self.offer_markup(
                    [p["id"] for p in (await self.bot_info())["products"] if p.get("chat_id") == request.chat.id]
                ))

        @router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
        async def on_member_joined(event: ChatMemberUpdated, bot: Bot) -> None:
            user, member = event.new_chat_member.user, event.new_chat_member
            if event.chat.id not in await product_chats() or user.is_bot:
                return
            if member.status in ("administrator", "creator") or event.from_user.id == bot.id:
                return
            if await self.has_access(user.id, chat_id=event.chat.id):
                return
            try:
                await bot.ban_chat_member(event.chat.id, user.id)
                await bot.unban_chat_member(event.chat.id, user.id, only_if_banned=True)
                log.info("%s вошёл в %s без оплаты и удалён", user.id, event.chat.id)
            except TelegramAPIError as exc:
                log.warning("Не удалось удалить %s из %s: %s", user.id, event.chat.id, exc)

        return router

    def paywall(self, products: Iterable[str] | None = None, *, allow_commands: Iterable[str] = ("paysupport", "terms"),
                exempt_users: Iterable[int] = (), text: str = PAYWALL_TEXT):
        """Middleware aiogram: в личке бот отвечает только тем, у кого оплачен доступ.

        products — какие товары открывают бота (None — любой оплаченный товар).
        """
        from aiogram import BaseMiddleware
        from aiogram.types import CallbackQuery, Message

        client = self
        products = list(products) if products else None
        allowed = {f"/{c.lstrip('/')}" for c in allow_commands}
        exempt = set(exempt_users) | self.admins

        class Paywall(BaseMiddleware):
            async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: dict[str, Any]) -> Any:
                if isinstance(event, Message):
                    chat, user = event.chat, event.from_user
                    if event.successful_payment or (event.text and event.text.split()[0].split("@")[0] in allowed):
                        return await handler(event, data)
                elif isinstance(event, CallbackQuery):
                    chat, user = (event.message.chat if event.message else None), event.from_user
                    if (event.data or "").startswith(CALLBACK_PREFIX):
                        return await handler(event, data)
                else:
                    return await handler(event, data)
                if user is None or (chat is not None and chat.type != "private") or user.id in exempt:
                    return await handler(event, data)
                try:
                    if await client.has_any_access(user.id, products):
                        return await handler(event, data)
                    markup, message_text = await client.offer_markup(products), text
                except (PlateshError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    log.warning("Platesh недоступен: %s", exc)
                    markup, message_text = None, UNAVAILABLE_TEXT
                if isinstance(event, CallbackQuery):
                    await event.answer()
                await data["bot"].send_message(user.id, message_text, reply_markup=markup)
                return None

        return Paywall()

    def install_paywall(self, dp: Any, products: Iterable[str] | None = None, **kwargs: Any) -> None:
        middleware = self.paywall(products, **kwargs)
        dp.message.outer_middleware(middleware)
        dp.callback_query.outer_middleware(middleware)


def verify_webhook(body: bytes, signature: str | None, secret: str) -> bool:
    """Проверка заголовка X-Platesh-Signature у уведомления на notify_url."""
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
