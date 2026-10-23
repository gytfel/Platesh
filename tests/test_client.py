"""client/platesh_client.py против настоящего HTTP API Platesh."""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest
from aiogram import Dispatcher
from aiogram.methods import AnswerPreCheckoutQuery
from aiogram.types import Chat, Message, PreCheckoutQuery, SuccessfulPayment, Update, User

from platesh.models import PAID
from platesh.web import create_app

from .conftest import BUYER_ID, make_bot

sys.path.insert(0, str(Path(__file__).parent.parent / "client"))
from platesh_client import PlateshClient, PlateshError, verify_webhook  # noqa: E402


@pytest.fixture
async def platesh(env, aiohttp_server):
    server = await aiohttp_server(create_app(env.cfg, env.registry, env.payments))
    client = PlateshClient(str(server.make_url("/")), api_key="secret-key", bot="ext")
    yield client
    await client.close()


async def test_create_and_get(platesh, env):
    inv = await platesh.create_invoice(method="crypto", amount=3, title="Тест", user_id=BUYER_ID)
    assert inv["status"] == "pending" and inv["pay_url"]
    assert (await platesh.get_invoice(inv["id"], refresh=True))["id"] == inv["id"]
    with pytest.raises(PlateshError, match="не найден"):
        await platesh.create_invoice(method="stars", product_id="nope")


async def test_stars_router(platesh, env):
    inv = await platesh.create_invoice(method="stars", amount=10, title="Донат", user_id=BUYER_ID)
    external_bot = make_bot("333333:own-bot-code")  # «чужой» бот со своим кодом
    dp = Dispatcher()
    dp.include_router(platesh.stars_router())

    query = PreCheckoutQuery(id="q", from_user=User(id=BUYER_ID, is_bot=False, first_name="A"),
                             currency="XTR", total_amount=10, invoice_payload=f"pl:{inv['id']}")
    await dp.feed_update(external_bot, Update(update_id=1, pre_checkout_query=query))
    assert external_bot.session.calls(AnswerPreCheckoutQuery)[-1].ok is True

    sp = SuccessfulPayment(currency="XTR", total_amount=10, invoice_payload=f"pl:{inv['id']}",
                           telegram_payment_charge_id="charge-1", provider_payment_charge_id="")
    msg = Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=BUYER_ID, type="private"),
                  from_user=User(id=BUYER_ID, is_bot=False, first_name="A", username="a"), successful_payment=sp)
    await dp.feed_update(external_bot, Update(update_id=2, message=msg))
    stored = await env.db.get_invoice(inv["id"])
    assert stored.status == PAID and stored.charge_id == "charge-1"

    # Счета, созданные не через Platesh, роутер не трогает.
    foreign = PreCheckoutQuery(id="q2", from_user=User(id=1, is_bot=False, first_name="B"),
                               currency="XTR", total_amount=10, invoice_payload="my-own-invoice")
    await dp.feed_update(external_bot, Update(update_id=3, pre_checkout_query=foreign))
    assert len(external_bot.session.calls(AnswerPreCheckoutQuery)) == 1


def test_verify_webhook():
    import hashlib
    import hmac

    body = b'{"event":"invoice_paid"}'
    sig = hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert verify_webhook(body, sig, "s")
    assert not verify_webhook(body, sig, "other")
    assert not verify_webhook(body, None, "s")
