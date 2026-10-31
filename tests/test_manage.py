"""Команды управления с сервера (./run.sh grant / access / revoke / payments / refund) против настоящего API."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from platesh.manage import ManageError, run_command
from platesh.web import create_app

from .conftest import BUYER_ID


@pytest.fixture
async def manage(env, aiohttp_server):
    server = await aiohttp_server(create_app(env.cfg, env.registry, env.payments))
    cfg = dataclasses.replace(env.cfg, server=dataclasses.replace(env.cfg.server, host="0.0.0.0", port=server.port))

    async def run(command, *params, bot=None, product=None):
        # Команды синхронные (urllib) — как в терминале, отдельно от цикла событий сервера.
        return await asyncio.to_thread(run_command, cfg, command, [str(p) for p in params], bot=bot, product=product)

    return run


async def test_grant_access_revoke(manage, env):
    with pytest.raises(ManageError, match="ботов несколько"):
        await manage("grant", BUYER_ID)
    with pytest.raises(ManageError, match="товаров несколько"):
        await manage("grant", BUYER_ID, bot="shop")
    with pytest.raises(ManageError, match="Telegram ID"):
        await manage("grant", "abc", bot="shop", product="vip")

    out = await manage("grant", BUYER_ID, 30, bot="shop", product="vip")
    assert out.startswith(f"✅ {BUYER_ID}: доступ к «VIP» до ")
    access = await env.db.get_access("shop", BUYER_ID, "vip")
    assert access is not None and access.is_active()

    out = await manage("access", BUYER_ID)
    assert "Бот «shop»: ✅ VIP (vip) — до " in out and "Бот «ext»: у" in out

    assert await manage("revoke", BUYER_ID, bot="shop", product="vip") == f"✅ {BUYER_ID}: доступ к «VIP» закрыт"
    assert "❌ VIP" in await manage("access", BUYER_ID, bot="shop")
    assert "не было доступа" in await manage("revoke", BUYER_ID, bot="shop", product="vip")


async def test_payments_and_refund(manage, env):
    assert await manage("payments") == "Оплат пока нет"
    inv = await env.payments.create_invoice(env.ext, method="stars", user_id=BUYER_ID, amount=25, title="Донат", stars_link=False)
    await env.payments.process_stars_payment(env.ext, payload=f"pl:{inv.id}", charge_id="tg-charge", total_amount=25,
                                             currency="XTR", user_id=BUYER_ID)

    out = await manage("payments", BUYER_ID)
    assert f"{BUYER_ID}  25 ⭐  Донат  · бот ext · счёт {inv.id}" in out
    assert await manage("refund", inv.id) == f"✅ Возврат 25 ⭐ пользователю {BUYER_ID} сделан, доступ закрыт"
    with pytest.raises(ManageError, match="только оплаченный"):
        await manage("refund", inv.id)


def test_platesh_not_running(env):
    cfg = dataclasses.replace(env.cfg, server=dataclasses.replace(env.cfg.server, host="127.0.0.1", port=1))
    with pytest.raises(ManageError, match="systemctl status platesh"):
        run_command(cfg, "access", [str(BUYER_ID)])
