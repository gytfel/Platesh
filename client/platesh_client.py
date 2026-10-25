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
from html import escape
from typing import Any, Awaitable, Callable, Iterable

import aiohttp

log = logging.getLogger("platesh_client")

PAYLOAD_PREFIX = "pl:"
CALLBACK_PREFIX = "platesh:"
ACCESS_TTL = 60  # сколько секунд помнить «доступ есть»
NO_ACCESS_TTL = 5  # и «доступа нет» (коротко, чтобы оплата криптой подхватывалась быстро)
BOT_INFO_TTL = 300  # цены и товары из config.yaml Platesh перечитываем раз в 5 минут

PAYWALL_TEXT = "🔒 Бот ограничен: нужна подписка. Выбери подписку:"
BUSINESS_TEXT = ("🔒 Бот подключён к твоему аккаунту, но сохранять переписку начнёт только после оформления подписки. "
                 "Выбери подписку:")
METHOD_TEXT = "Выбери способ оплаты:"
BACK_TEXT = "‹ Назад"
BUSINESS_REMIND_EVERY = 6 * 3600  # напоминать владельцу аккаунта не чаще раза в 6 часов
UNAVAILABLE_TEXT = "Сервис оплаты временно недоступен, попробуй через минуту."
ASSET_UNAVAILABLE_TEXT = "Оплата в {asset} сейчас недоступна. Выбери другую монету."
# Монеты CryptoBot на кнопках и знаки валют для цены в фиате («5 $»).
ASSET_NAMES = {"SOL": "Solana", "BTC": "Bitcoin", "ETH": "Ethereum", "TRX": "TRON", "LTC": "Litecoin"}
ASSET_ICONS = {"TON": "💎", "USDT": "💵", "USDC": "💵", "SOL": "◎", "BTC": "₿", "ETH": "Ξ"}
CURRENCY_SIGNS = {"USD": "$", "EUR": "€", "RUB": "₽"}


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
        self._bot_info_until = 0.0
        self._business_owners: dict[str, int] = {}  # business_connection_id -> владелец аккаунта
        self._business_reminded: dict[int, float] = {}
        # Что показывать на кнопках «Назад»/«Продлить» — задаёт paywall().
        self._offer_products: list[str] | None = None
        self._offer_text = PAYWALL_TEXT
        self._method_text = METHOD_TEXT

    @classmethod
    def from_env(cls, **kwargs: Any) -> PlateshClient:
        """Настройки из переменных, которые Platesh передаёт боту при запуске (run: в config.yaml)."""
        values = {name: (os.environ.get(name) or "").strip() for name in ("PLATESH_URL", "PLATESH_KEY", "PLATESH_BOT")}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise PlateshError(
                f"Не заданы {', '.join(missing)}. Запускай бота через Platesh (run: в config.yaml), "
                "впиши их в .env бота или создай клиент явно: PlateshClient(url, api_key=..., bot=...)"
            )
        admins = []
        for item in os.environ.get("PLATESH_ADMINS", "").replace(" ", "").split(","):
            if not item:
                continue
            if not item.lstrip("-").isdigit():
                raise PlateshError(f"PLATESH_ADMINS: «{item}» — не Telegram ID. Нужны числа через запятую")
            admins.append(int(item))
        return cls(values["PLATESH_URL"], values["PLATESH_KEY"], values["PLATESH_BOT"], admins=admins, **kwargs)

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
        asset: str | None = None,
    ) -> dict[str, Any]:
        """asset — монета (TON, USDT...), если цена в фиате и покупатель выбрал, чем платить."""
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
            "asset": asset,
        }
        return await self._request("POST", "/api/v1/invoices", json={k: v for k, v in body.items() if v is not None})

    async def get_invoice(self, invoice_id: str, *, refresh: bool = False) -> dict[str, Any]:
        params = {"refresh": "1"} if refresh else None
        return await self._request("GET", f"/api/v1/invoices/{invoice_id}", params=params)

    async def bot_info(self) -> dict[str, Any]:
        """Настройки бота из config.yaml: способы оплаты, товары, цены, закрытые чаты."""
        if self._bot_info is None or time.monotonic() > self._bot_info_until:
            bots = await self._request("GET", "/api/v1/bots")
            info = next((b for b in bots if b["name"] == self.bot), None)
            if info is None:
                raise PlateshError(f"бот «{self.bot}» не найден в Platesh")
            self._bot_info, self._bot_info_until = info, time.monotonic() + BOT_INFO_TTL
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

    async def _offered(self, products: Iterable[str] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Товары, которые можно купить хотя бы одним включённым способом."""
        info = await self.bot_info()
        wanted = set(products) if products else None
        offered = [p for p in info["products"]
                   if (wanted is None or p["id"] in wanted) and _methods(info, p)]
        return info, offered

    async def offer_markup(self, products: Iterable[str] | None = None):
        """Шаг 1 — кнопки подписок «📅 На 30 дней». Цены и способ оплаты — следующим шагом."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        _, offered = await self._offered(products)
        rows = [[InlineKeyboardButton(text=f"📅 {_plan_names(p, offered)[0]}", callback_data=f"{CALLBACK_PREFIX}plan:{p['id']}")]
                for p in offered]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def method_offer(self, product_id: str, *, back: bool = True) -> tuple[str, Any] | None:
        """Шаг 2 — текст и кнопки «⭐ Telegram Stars — 350 ⭐» / «💎 CryptoBot — 4 TON» для одной подписки."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        info, offered = await self._offered(None)
        product = next((p for p in offered if p["id"] == product_id), None)
        if product is None:
            return None
        rows: list[list[Any]] = []
        coins: list[Any] = []  # монеты — по две кнопки в ряд
        for method, label, amount in _methods(info, product):
            button = InlineKeyboardButton(text=f"{label} — {amount}", callback_data=f"{CALLBACK_PREFIX}pay:{product_id}:{method}")
            if method.startswith("crypto:"):
                coins.append(button)
            else:
                rows.append([button])
        rows += [coins[i:i + 2] for i in range(0, len(coins), 2)]
        if back:
            rows.append([InlineKeyboardButton(text=BACK_TEXT, callback_data=f"{CALLBACK_PREFIX}plans")])
        text = f"📅 <b>{escape(_plan_names(product, offered)[1])}</b>\n\n{self._method_text}"
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

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

        async def show(callback: CallbackQuery, text: str, markup: Any, parse_mode: str | None, *, edit: bool) -> None:
            """Меняем то же сообщение (меньше мусора в чате); не вышло — присылаем новое."""
            await callback.answer()
            if edit and isinstance(callback.message, Message):
                try:
                    await callback.message.edit_text(text, reply_markup=markup, **_parse_mode(parse_mode))
                    return
                except TelegramBadRequest as exc:
                    if "not modified" in str(exc):
                        return
            await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup, **_parse_mode(parse_mode))

        async def show_methods(callback: CallbackQuery, *, edit: bool) -> None:
            product_id = callback.data.split(":", 2)[2]
            try:
                offer = await self.method_offer(product_id, back=True)
            except (PlateshError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Platesh недоступен: %s", exc)
                await callback.answer(UNAVAILABLE_TEXT, show_alert=True)
                return
            if offer is None:
                await callback.answer("Эта подписка больше не продаётся", show_alert=True)
                return
            await show(callback, offer[0], offer[1], "HTML", edit=edit)

        @router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}plan:"))
        async def on_plan(callback: CallbackQuery) -> None:
            await show_methods(callback, edit=True)

        @router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}buy:"))
        async def on_buy(callback: CallbackQuery) -> None:
            # «Продлить» в напоминании Platesh: сразу выбор способа оплаты, новым сообщением.
            await show_methods(callback, edit=False)

        @router.callback_query(F.data == f"{CALLBACK_PREFIX}plans")
        async def on_plans(callback: CallbackQuery) -> None:
            try:
                markup = await self.offer_markup(self._offer_products)
            except (PlateshError, aiohttp.ClientError, asyncio.TimeoutError):
                await callback.answer(UNAVAILABLE_TEXT, show_alert=True)
                return
            await show(callback, self._offer_text, markup, None, edit=True)

        @router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}pay:"))
        async def on_pay(callback: CallbackQuery) -> None:
            _, _, product_id, method, asset = (callback.data.split(":", 4) + ["", "", ""])[:5]
            try:
                await self.create_invoice(method=method, product_id=product_id, user_id=callback.from_user.id,
                                          send=True, asset=asset or None)
            except (PlateshError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Не удалось создать счёт: %s", exc)
                text = ASSET_UNAVAILABLE_TEXT.format(asset=_asset_name(asset)) if asset else UNAVAILABLE_TEXT
                await callback.answer(text, show_alert=True)
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

    async def _business_owner(self, bot: Any, connection_id: str) -> int | None:
        """Владелец бизнес-аккаунта, к которому подключён бот (кэшируется)."""
        if connection_id not in self._business_owners:
            from aiogram.exceptions import TelegramAPIError

            try:
                connection = await bot.get_business_connection(connection_id)
            except TelegramAPIError as exc:
                log.warning("Не удалось узнать владельца бизнес-подключения %s: %s", connection_id, exc)
                return None
            self._business_owners[connection_id] = connection.user.id
        return self._business_owners[connection_id]

    def paywall(self, products: Iterable[str] | None = None, *, allow_commands: Iterable[str] = ("paysupport", "terms"),
                exempt_users: Iterable[int] = (), text: str = PAYWALL_TEXT, business_text: str = BUSINESS_TEXT,
                method_text: str = METHOD_TEXT):
        """Middleware aiogram: бот работает только для тех, у кого оплачен доступ.

        - сообщения и кнопки в личке: без оплаты — предложение оплатить;
        - Telegram Business (бот подключён к аккаунту пользователя): пока владелец аккаунта не оплатил,
          его переписка боту не передаётся (бот её не обрабатывает и не сохраняет), а владелец
          получает предложение оплатить (не чаще раза в 6 часов).

        products — какие товары открывают бота (None — любой оплаченный товар).
        text — сообщение с кнопками подписок; method_text — подпись к выбору способа оплаты.
        """
        from aiogram import BaseMiddleware
        from aiogram.types import BusinessConnection, BusinessMessagesDeleted, CallbackQuery, Message

        client = self
        products = list(products) if products else None
        allowed = {f"/{c.lstrip('/')}" for c in allow_commands}
        exempt = set(exempt_users) | self.admins
        self._offer_products, self._offer_text, self._method_text = products, text, method_text

        async def remind_owner(bot: Any, owner_id: int) -> None:
            last = client._business_reminded.get(owner_id)
            if last is not None and time.monotonic() - last < BUSINESS_REMIND_EVERY:
                return
            client._business_reminded[owner_id] = time.monotonic()
            with suppress(Exception):
                await bot.send_message(owner_id, business_text, reply_markup=await client.offer_markup(products))

        async def business(handler: Callable[..., Awaitable[Any]], event: Any, data: dict[str, Any]) -> Any:
            bot = data["bot"]
            if isinstance(event, BusinessConnection):
                client._business_owners[event.id] = event.user.id
                owner = event.user.id
            else:
                owner = await client._business_owner(bot, event.business_connection_id)
            if owner is None or owner in exempt:
                return await handler(event, data)
            try:
                paid = await client.has_any_access(owner, products)
            except (PlateshError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Platesh недоступен: лучше временно поработать бесплатно, чем потерять переписку оплатившего.
                log.warning("Platesh недоступен, бизнес-сообщение пропущено без проверки: %s", exc)
                return await handler(event, data)
            if paid:
                return await handler(event, data)
            if isinstance(event, BusinessConnection):
                # Само подключение пропускаем — код бота его запомнит; предложение оплатить — следом.
                result = await handler(event, data)
                if event.is_enabled:
                    await remind_owner(bot, owner)
                return result
            await remind_owner(bot, owner)
            return None  # без оплаты переписку не обрабатываем

        class Paywall(BaseMiddleware):
            async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: dict[str, Any]) -> Any:
                if isinstance(event, (BusinessConnection, BusinessMessagesDeleted)) or (
                    isinstance(event, Message) and event.business_connection_id
                ):
                    return await business(handler, event, data)
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
        for observer in ("message", "callback_query", "business_connection", "business_message",
                         "edited_business_message", "deleted_business_messages"):
            if hasattr(dp, observer):  # Telegram Business есть в aiogram 3.7+
                getattr(dp, observer).outer_middleware(middleware)


def _asset_name(asset: str) -> str:
    return ASSET_NAMES.get(asset, asset)


def _methods(info: dict[str, Any], product: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Способы оплаты товара: (method, подпись, цена). Цена в фиате (5 USD) — по кнопке на каждую монету."""
    methods = []
    if product.get("price_stars") is not None and "stars" in info["payments"]:
        methods.append(("stars", "⭐ Telegram Stars", f"{product['price_stars']} ⭐"))
    if product.get("price_crypto") is not None and "crypto" in info["payments"]:
        currency = info["crypto_currency"]
        price = f"{product['price_crypto']} {CURRENCY_SIGNS.get(currency, currency)}"
        assets = info.get("crypto_assets") or []  # старый Platesh этого поля не присылает
        if assets:
            methods += [(f"crypto:{a}", f"{ASSET_ICONS.get(a, '🪙')} {_asset_name(a)}", price) for a in assets]
        else:
            methods.append(("crypto", "💎 CryptoBot", price))
    return methods


def _days(days: int) -> str:
    if days % 10 == 1 and days % 100 != 11:
        return f"{days} день"
    if 2 <= days % 10 <= 4 and not 12 <= days % 100 <= 14:
        return f"{days} дня"
    return f"{days} дней"


def _plan_names(product: dict[str, Any], offered: list[dict[str, Any]]) -> tuple[str, str]:
    """(коротко для кнопки, полностью для заголовка). Подписки различаем по сроку, при одинаковом сроке — по названию."""
    days = product.get("days")
    if sum(1 for p in offered if p.get("days") == days) > 1:
        return product["title"], f"{product['title']} — {_days(days)}" if days else product["title"]
    if days:
        return f"На {_days(days)}", f"Подписка на {_days(days)}"
    return "Навсегда", "Доступ навсегда"


def _parse_mode(parse_mode: str | None) -> dict[str, Any]:
    return {"parse_mode": parse_mode} if parse_mode else {}


def verify_webhook(body: bytes, signature: str | None, secret: str) -> bool:
    """Проверка заголовка X-Platesh-Signature у уведомления на notify_url."""
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
