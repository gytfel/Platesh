from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .texts import format_amount, format_price

PENDING = "pending"
PAID = "paid"
EXPIRED = "expired"
REFUNDED = "refunded"
FAILED = "failed"

PAYLOAD_PREFIX = "pl:"


@dataclass
class Invoice:
    id: str
    bot: str
    method: str
    amount: Decimal
    currency: str
    title: str
    description: str = ""
    user_id: int | None = None
    product_id: str | None = None
    status: str = PENDING
    pay_url: str | None = None
    external_id: str | None = None
    charge_id: str | None = None
    paid_asset: str | None = None
    paid_amount: str | None = None
    payload: str | None = None
    created_at: int = 0
    paid_at: int | None = None

    @property
    def tg_payload(self) -> str:
        """Строка, которую мы кладём в счёт Telegram/CryptoBot, чтобы потом найти его у себя."""
        return f"{PAYLOAD_PREFIX}{self.id}"

    @property
    def price(self) -> str:
        return format_price(self.amount, self.currency)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["amount"] = format_amount(self.amount)
        return data


@dataclass
class Access:
    """Право пользователя на товар. expires_at=None — навсегда."""

    bot: str
    user_id: int
    product_id: str
    expires_at: int | None
    reminded: bool = False
    revoked: bool = False
    updated_at: int = 0

    def is_active(self, now: float | None = None) -> bool:
        if self.revoked:
            return False
        return self.expires_at is None or self.expires_at > (time.time() if now is None else now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "expires_at": self.expires_at,
            "active": self.is_active(),
        }


def parse_payload(payload: str | None) -> str | None:
    if payload and payload.startswith(PAYLOAD_PREFIX):
        return payload[len(PAYLOAD_PREFIX):] or None
    return None
