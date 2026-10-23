"""Клиент Platesh для ботов со своим кодом (mode: external).

Скопируй этот файл в проект своего бота. Нужен только aiohttp (для stars_router — aiogram 3).

    from platesh_client import PlateshClient

    platesh = PlateshClient("http://127.0.0.1:8080", api_key="...", bot="mybot")

    # 1) Счёт на товар из config.yaml — Platesh сам пришлёт его пользователю:
    await platesh.create_invoice(user_id=123, method="stars", product_id="vip", send=True)

    # 2) Или произвольная сумма — получаешь ссылку и показываешь её сам:
    inv = await platesh.create_invoice(user_id=123, method="crypto", amount="9.99", title="Подписка")
    print(inv["pay_url"])

    # 3) Для оплаты Stars подключи роутер — он подтверждает платежи через Platesh:
    dp.include_router(platesh.stars_router())

Уведомление об оплате придёт на notify_url (см. README), проверить подпись — verify_webhook().
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any

import aiohttp

PAYLOAD_PREFIX = "pl:"


class PlateshError(Exception):
    pass


class PlateshClient:
    def __init__(self, base_url: str, api_key: str, bot: str, *, timeout: float = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.bot = bot
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

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

    async def refund(self, invoice_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/invoices/{invoice_id}/refund")

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

    def stars_router(self):
        """Роутер aiogram 3: подтверждает и учитывает оплаты Stars по счетам Platesh.

        Счета, созданные не через Platesh (payload без префикса "pl:"), он не трогает.
        """
        from aiogram import F, Router
        from aiogram.types import Message, PreCheckoutQuery

        router = Router(name="platesh")

        @router.pre_checkout_query(F.invoice_payload.startswith(PAYLOAD_PREFIX))
        async def on_pre_checkout(query: PreCheckoutQuery) -> None:
            try:
                error = await self.pre_checkout(query.invoice_payload, query.currency, query.total_amount)
            except (PlateshError, aiohttp.ClientError, TimeoutError):
                error = "Платёжный сервис временно недоступен, попробуйте позже."
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
                    return
                except (aiohttp.ClientError, TimeoutError):
                    continue
            raise PlateshError(f"Platesh недоступен, оплата {sp.telegram_payment_charge_id} не учтена")

        return router


def verify_webhook(body: bytes, signature: str | None, secret: str) -> bool:
    """Проверка заголовка X-Platesh-Signature у уведомления на notify_url."""
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
