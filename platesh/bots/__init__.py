from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Dispatcher
from aiogram.types import TelegramObject

from ..payments import PaymentService
from .admin import create_admin_router
from .registry import BotRegistry, BotRuntime
from .shop import create_shop_router


class ContextMiddleware(BaseMiddleware):
    """Подставляет в обработчики настройки того бота, который получил апдейт."""

    def __init__(self, registry: BotRegistry, payments: PaymentService) -> None:
        self.registry = registry
        self.payments = payments

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        rt = self.registry.by_id(data["bot"].id)
        if rt is None:
            return None
        data["rt"] = rt
        data["payments"] = self.payments
        return await handler(event, data)


def build_dispatcher(registry: BotRegistry, payments: PaymentService) -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(ContextMiddleware(registry, payments))
    dp.include_routers(create_admin_router(), create_shop_router())
    return dp


__all__ = ["BotRegistry", "BotRuntime", "build_dispatcher"]
