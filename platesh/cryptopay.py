"""Минимальный клиент Crypto Pay API (@CryptoBot): https://help.crypt.bot/crypto-pay-api"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from decimal import Decimal
from typing import Any

import aiohttp

from .net import proxy_connector, proxy_errors
from .texts import format_amount

log = logging.getLogger(__name__)

MAINNET_URL = "https://pay.crypt.bot/api"
TESTNET_URL = "https://testnet-pay.crypt.bot/api"


class CryptoPayError(Exception):
    def __init__(self, method: str, code: int | None, name: str) -> None:
        super().__init__(f"Crypto Pay {method}: {name}" + (f" ({code})" if code is not None else ""))
        self.method = method
        self.code = code
        self.name = name


def verify_signature(token: str, body: bytes, signature: str | None) -> bool:
    """Проверка заголовка crypto-pay-api-signature у вебхука CryptoBot."""
    if not signature:
        return False
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


class CryptoPayClient:
    def __init__(self, token: str, *, testnet: bool = False, base_url: str | None = None, proxy: str | None = None) -> None:
        self.token = token
        self.testnet = testnet
        self.proxy = proxy
        self.base_url = (base_url or (TESTNET_URL if testnet else MAINNET_URL)).rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=proxy_connector(self.proxy) if self.proxy else None,
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def call(self, method: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.base_url}/{method}",
                json=params,
                headers={"Crypto-Pay-API-Token": self.token},
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    raise CryptoPayError(method, resp.status, f"HTTP {resp.status}") from None
        except (aiohttp.ClientError, asyncio.TimeoutError, *proxy_errors()) as exc:
            raise CryptoPayError(method, None, f"нет связи с CryptoBot: {exc}") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error", {}) if isinstance(data, dict) else {}
            if isinstance(error, str):
                error = {"name": error}
            raise CryptoPayError(method, error.get("code", resp.status), error.get("name", "unknown error"))
        return data["result"]

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def create_invoice(
        self,
        amount: Decimal,
        *,
        currency: str,
        is_fiat: bool,
        accepted_assets: tuple[str, ...] = (),
        description: str | None = None,
        payload: str | None = None,
        expires_in: int | None = None,
        paid_btn_url: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "amount": format_amount(amount),
            "description": (description or "")[:1024] or None,
            "payload": payload,
            "expires_in": expires_in,
            "allow_comments": False,
        }
        if is_fiat:
            params["currency_type"] = "fiat"
            params["fiat"] = currency
            if accepted_assets:
                params["accepted_assets"] = ",".join(accepted_assets)
        else:
            params["currency_type"] = "crypto"
            params["asset"] = currency
        if paid_btn_url:
            params["paid_btn_name"] = "openBot"
            params["paid_btn_url"] = paid_btn_url
        return await self.call("createInvoice", **params)

    async def get_invoices(self, invoice_ids: list[int | str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for start in range(0, len(invoice_ids), 100):
            chunk = invoice_ids[start:start + 100]
            result = await self.call(
                "getInvoices", invoice_ids=",".join(str(i) for i in chunk), count=len(chunk)
            )
            items.extend(result.get("items", []) if isinstance(result, dict) else result)
        return items


def invoice_url(invoice: dict[str, Any]) -> str | None:
    """Ссылка на оплату (поле pay_url в API помечено устаревшим, берём новое)."""
    return invoice.get("bot_invoice_url") or invoice.get("pay_url") or invoice.get("mini_app_invoice_url")
