"""Уведомления об оплатах: админам в Telegram и на ваш сервер (notify_url)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from aiogram.exceptions import TelegramAPIError

from .config import Config
from .db import Database
from .models import Access, Invoice
from .texts import format_date

if TYPE_CHECKING:
    from .bots.registry import BotRuntime

log = logging.getLogger(__name__)

WEBHOOK_RETRY_DELAYS = (0, 5, 30, 120, 600)
SIGNATURE_HEADER = "X-Platesh-Signature"


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Notifier:
    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self._tasks: set[asyncio.Task[Any]] = set()
        self._session: aiohttp.ClientSession | None = None
        self.retry_delays = WEBHOOK_RETRY_DELAYS

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self, grace: float = 5.0) -> None:
        if self._tasks:
            # Даём досчитаться уведомлениям, которые уже в пути; ретраи вебхуков обрываем.
            await asyncio.wait(list(self._tasks), timeout=grace)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ admins

    async def alert(self, rt: BotRuntime, text: str) -> None:
        for admin_id in rt.cfg.admins:
            try:
                await rt.bot.send_message(admin_id, text, disable_web_page_preview=True)
            except TelegramAPIError as exc:
                # Чаще всего админ ещё не нажал /start в этом боте.
                log.warning("Не удалось уведомить админа %s через @%s: %s", admin_id, rt.username, exc)

    async def _buyer(self, bot: str, user_id: int | None) -> dict[str, Any]:
        user = await self.db.get_user(bot, user_id) if user_id else None
        return {
            "id": user_id,
            "username": user.get("username") if user else None,
            "first_name": user.get("first_name") if user else None,
        }

    def _post(self, rt: BotRuntime, event: str, data: dict[str, Any]) -> None:
        if rt.cfg.notify_url:
            body = {"event": event, **data, "bot": {"name": rt.name, "id": rt.id, "username": rt.username}}
            self._spawn(self._deliver_webhook(rt.cfg.notify_url, event, body))

    async def invoice_event(self, rt: BotRuntime, inv: Invoice, event: str, access: Access | None = None) -> None:
        buyer = await self._buyer(inv.bot, inv.user_id)
        text = self._format(rt, inv, event, buyer)
        if access is not None and access.expires_at:
            text += f"\nДоступ до: {format_date(access.expires_at)}"
        await self.alert(rt, text)
        data: dict[str, Any] = {"invoice": inv.to_dict(), "user": buyer}
        if access is not None:
            data["access"] = access.to_dict()
        self._post(rt, event, data)

    def payment_received(self, rt: BotRuntime, inv: Invoice, access: Access | None = None) -> None:
        self._spawn(self.invoice_event(rt, inv, "invoice_paid", access))

    def refunded(self, rt: BotRuntime, inv: Invoice) -> None:
        self._spawn(self.invoice_event(rt, inv, "invoice_refunded"))

    def access_expired(self, rt: BotRuntime, access: Access) -> None:
        async def send() -> None:
            self._post(rt, "access_expired", {"access": access.to_dict(), "user": await self._buyer(rt.name, access.user_id)})

        self._spawn(send())

    @staticmethod
    def _format(rt: BotRuntime, inv: Invoice, event: str, buyer: dict[str, Any]) -> str:
        head = "💰 <b>Новая оплата</b>" if event == "invoice_paid" else "↩️ <b>Возврат</b>"
        who = f"<code>{inv.user_id}</code>" if inv.user_id else "—"
        if buyer.get("username"):
            who += f" @{html.escape(buyer['username'])}"
        elif buyer.get("first_name"):
            who += f" {html.escape(buyer['first_name'])}"
        method = "Telegram Stars" if inv.method == "stars" else "CryptoBot"
        amount = html.escape(inv.price)
        if inv.paid_asset and inv.paid_amount:
            amount += f" (оплачено {html.escape(inv.paid_amount)} {html.escape(inv.paid_asset)})"
        return (
            f"{head}\n\n"
            f"Бот: @{rt.username}\n"
            f"Товар: {html.escape(inv.title)}\n"
            f"Сумма: {amount}\n"
            f"Способ: {method}\n"
            f"Покупатель: {who}\n"
            f"Счёт: <code>{inv.id}</code>"
        )

    # ------------------------------------------------------------------ webhooks

    async def _deliver_webhook(self, url: str, event: str, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "X-Platesh-Event": event}
        secret = self.cfg.server.webhook_secret
        if secret:
            headers[SIGNATURE_HEADER] = sign(secret, body)
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=15))
        for attempt, delay in enumerate(self.retry_delays, 1):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with self._session.post(url, data=body, headers=headers) as resp:
                    if 200 <= resp.status < 300:
                        return
                    log.warning("notify_url %s ответил %s (попытка %s)", url, resp.status, attempt)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("notify_url %s недоступен: %s (попытка %s)", url, exc, attempt)
        log.error("Не удалось доставить событие %s на %s", event, url)
