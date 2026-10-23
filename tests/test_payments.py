from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    CreateChatInviteLink,
    CreateInvoiceLink,
    RefundStarPayment,
    SendInvoice,
    SendMessage,
)
from aiohttp import web

from platesh.models import EXPIRED, FAILED, PAID, PENDING, REFUNDED
from platesh.notifier import SIGNATURE_HEADER
from platesh.payments import PaymentError

from .conftest import ADMIN_ID, BUYER_ID, CRYPTO_TOKEN, build_env, make_raw_config


def messages_to(session, chat_id):
    return [m.text for m in session.calls(SendMessage) if m.chat_id == chat_id]


async def test_stars_full_flow(env):
    product = env.shop.cfg.products["vip"]
    inv = await env.payments.create_invoice(env.shop, method="stars", user_id=BUYER_ID, product=product)
    assert inv.amount == Decimal(100) and inv.currency == "XTR" and inv.status == PENDING
    assert inv.pay_url == "https://t.me/$invoice-link"
    link_call = env.session().calls(CreateInvoiceLink)[0]
    assert link_call.payload == f"pl:{inv.id}" and link_call.prices[0].amount == 100 and link_call.currency == "XTR"

    assert await env.payments.validate_pre_checkout(env.shop, inv.tg_payload, "XTR", 100) is None
    assert await env.payments.validate_pre_checkout(env.shop, inv.tg_payload, "XTR", 1) is not None
    assert await env.payments.validate_pre_checkout(env.shop, "pl:nope", "XTR", 100) is not None
    # Счёт одного бота нельзя оплатить в другом.
    assert await env.payments.validate_pre_checkout(env.ext, inv.tg_payload, "XTR", 100) is not None

    paid = await env.payments.process_stars_payment(
        env.shop, payload=inv.tg_payload, charge_id="ch1", total_amount=100, currency="XTR", user_id=BUYER_ID
    )
    assert paid.status == PAID and paid.charge_id == "ch1" and paid.paid_at
    await env.notifier.wait_idle()

    session = env.session()
    buyer_msgs = messages_to(session, BUYER_ID)
    assert buyer_msgs[0] == "Добро пожаловать!"
    assert "https://t.me/+one-time" in buyer_msgs[1]
    invite = session.calls(CreateChatInviteLink)[0]
    # Managed-бот выдаёт ссылку «по заявке»: войдёт только тот, у кого оплачен доступ.
    assert invite.chat_id == -100123 and invite.creates_join_request and invite.member_limit is None
    assert all(m.protect_content for m in session.calls(SendMessage) if m.chat_id == BUYER_ID)
    admin_msgs = messages_to(session, ADMIN_ID)
    assert len(admin_msgs) == 1 and "Новая оплата" in admin_msgs[0] and inv.id in admin_msgs[0]

    # После оплаты счёт больше не принимает платежи.
    assert await env.payments.validate_pre_checkout(env.shop, inv.tg_payload, "XTR", 100) is not None


async def test_stars_repeated_event_is_idempotent_and_double_payment_refunded(env):
    inv = await env.payments.create_invoice(env.shop, method="stars", user_id=BUYER_ID, product=env.shop.cfg.products["vip"])
    args = dict(payload=inv.tg_payload, total_amount=100, currency="XTR", user_id=BUYER_ID)
    await env.payments.process_stars_payment(env.shop, charge_id="ch1", **args)
    await env.payments.process_stars_payment(env.shop, charge_id="ch1", **args)  # тот же апдейт повторно
    assert env.session().calls(RefundStarPayment) == []
    assert len(env.session().calls(CreateChatInviteLink)) == 1

    await env.payments.process_stars_payment(env.shop, charge_id="ch2", **args)  # вторая оплата того же счёта
    refunds = env.session().calls(RefundStarPayment)
    assert len(refunds) == 1 and refunds[0].telegram_payment_charge_id == "ch2"
    assert len(env.session().calls(CreateChatInviteLink)) == 1


async def test_stars_unknown_payment_is_refunded(env):
    result = await env.payments.process_stars_payment(
        env.shop, payload="pl:unknown", charge_id="x", total_amount=10, currency="XTR", user_id=BUYER_ID
    )
    assert result is None
    assert env.session().calls(RefundStarPayment)[0].user_id == BUYER_ID
    assert any("возвращены" in t for t in messages_to(env.session(), ADMIN_ID))


async def test_stars_amount_must_be_integer(env):
    with pytest.raises(PaymentError):
        await env.payments.create_invoice(env.shop, method="stars", amount="1.5", title="x")
    with pytest.raises(PaymentError):
        await env.payments.create_invoice(env.shop, method="stars", amount="0", title="x")
    with pytest.raises(PaymentError):
        await env.payments.create_invoice(env.shop, method="stars", amount="10")  # без title


async def test_method_not_priced_for_product(env):
    with pytest.raises(PaymentError):
        await env.payments.create_invoice(env.shop, method="crypto", product=env.shop.cfg.products["stars_only"])


async def test_crypto_flow_via_polling(env):
    product = env.shop.cfg.products["vip"]
    inv = await env.payments.create_invoice(env.shop, method="crypto", user_id=BUYER_ID, product=product)
    crypto = env.crypto()
    created = crypto.created[0]
    assert created["amount"] == Decimal("4.99") and created["currency"] == "USD" and created["is_fiat"]
    assert created["accepted_assets"] == ("USDT", "TON") and created["payload"] == f"pl:{inv.id}"
    assert created["paid_btn_url"] == "https://t.me/shop_bot"
    assert inv.external_id == "900" and inv.pay_url.endswith("IV900")

    await env.payments.poll_crypto()
    assert (await env.db.get_invoice(inv.id)).status == PENDING

    crypto.set_status("900", "paid", paid_asset="USDT", paid_amount="4.99")
    await env.payments.poll_crypto()
    await env.payments.poll_crypto()  # повторный опрос не выдаёт товар второй раз
    stored = await env.db.get_invoice(inv.id)
    assert stored.status == PAID and stored.paid_asset == "USDT" and stored.charge_id == "IV900"
    assert len(env.session().calls(CreateChatInviteLink)) == 1
    await env.notifier.wait_idle()
    assert "оплачено 4.99 USDT" in messages_to(env.session(), ADMIN_ID)[0]


async def test_crypto_expired(env):
    inv = await env.payments.create_invoice(env.shop, method="crypto", user_id=BUYER_ID, product=env.shop.cfg.products["vip"])
    env.crypto().set_status(inv.external_id, "expired")
    refreshed = await env.payments.refresh(inv)
    assert refreshed.status == EXPIRED


async def test_crypto_creation_failure_marks_invoice_failed(env):
    from platesh.cryptopay import CryptoPayError

    env.crypto().fail = CryptoPayError("createInvoice", 400, "AMOUNT_TOO_SMALL")
    with pytest.raises(PaymentError):
        await env.payments.create_invoice(env.shop, method="crypto", amount="0.001", title="x")
    [inv] = await env.db.list_invoices()
    assert inv.status == FAILED


async def test_crypto_webhook(env):
    inv = await env.payments.create_invoice(env.shop, method="crypto", user_id=BUYER_ID, product=env.shop.cfg.products["vip"])
    item = env.crypto().set_status(inv.external_id, "paid", paid_asset="TON", paid_amount="1.2")
    body = json.dumps({"update_id": 1, "update_type": "invoice_paid", "payload": item}).encode()
    secret = hashlib.sha256(CRYPTO_TOKEN.encode()).digest()
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()

    assert await env.payments.handle_crypto_webhook(body, "bad") is False
    assert (await env.db.get_invoice(inv.id)).status == PENDING
    assert await env.payments.handle_crypto_webhook(body, signature) is True
    assert (await env.db.get_invoice(inv.id)).status == PAID
    # Повторная доставка вебхука ничего не ломает.
    assert await env.payments.handle_crypto_webhook(body, signature) is True
    assert len(env.session().calls(CreateChatInviteLink)) == 1


async def test_reuse_pending_invoice(env):
    product = env.shop.cfg.products["vip"]
    a = await env.payments.create_invoice(env.shop, method="crypto", user_id=BUYER_ID, product=product, reuse=True)
    b = await env.payments.create_invoice(env.shop, method="crypto", user_id=BUYER_ID, product=product, reuse=True)
    c = await env.payments.create_invoice(env.shop, method="crypto", user_id=BUYER_ID + 1, product=product, reuse=True)
    assert a.id == b.id != c.id
    assert len(env.crypto().created) == 2


async def test_send_invoice(env):
    stars = await env.payments.create_invoice(env.shop, method="stars", user_id=BUYER_ID, product=env.shop.cfg.products["vip"], stars_link=False)
    assert stars.pay_url is None
    await env.payments.send_invoice(env.shop, stars, BUYER_ID)
    sent = env.session().calls(SendInvoice)[0]
    assert sent.payload == stars.tg_payload and sent.prices[0].amount == 100

    crypto = await env.payments.create_invoice(env.shop, method="crypto", user_id=BUYER_ID, product=env.shop.cfg.products["vip"])
    await env.payments.send_invoice(env.shop, crypto, BUYER_ID)
    msg = env.session().calls(SendMessage)[-1]
    buttons = [b for row in msg.reply_markup.inline_keyboard for b in row]
    assert buttons[0].url == crypto.pay_url and buttons[1].callback_data == f"chk:{crypto.id}"


async def test_refund(env):
    inv = await env.payments.create_invoice(env.shop, method="stars", user_id=BUYER_ID, product=env.shop.cfg.products["vip"])
    with pytest.raises(PaymentError):
        await env.payments.refund(inv)  # ещё не оплачен
    await env.payments.process_stars_payment(env.shop, payload=inv.tg_payload, charge_id="ch1", total_amount=100, currency="XTR", user_id=BUYER_ID)
    refunded = await env.payments.refund(await env.db.get_invoice(inv.id))
    assert refunded.status == REFUNDED
    call = env.session().calls(RefundStarPayment)[0]
    assert call.user_id == BUYER_ID and call.telegram_payment_charge_id == "ch1"


async def test_delivery_failure_alerts_admin_but_keeps_payment(env):
    env.session().errors[CreateChatInviteLink] = TelegramBadRequest(CreateChatInviteLink(chat_id=1), "not enough rights")
    inv = await env.payments.create_invoice(env.shop, method="stars", user_id=BUYER_ID, product=env.shop.cfg.products["vip"])
    await env.payments.process_stars_payment(env.shop, payload=inv.tg_payload, charge_id="c", total_amount=100, currency="XTR", user_id=BUYER_ID)
    assert (await env.db.get_invoice(inv.id)).status == PAID
    await env.notifier.wait_idle()
    admin = messages_to(env.session(), ADMIN_ID)
    assert any("выдать покупку не удалось" in t for t in admin)
    assert any("Новая оплата" in t for t in admin)


async def test_notify_url_webhook_is_signed(tmp_path, aiohttp_server):
    received = []

    async def hook(request):
        received.append((await request.read(), request.headers.get(SIGNATURE_HEADER)))
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/hook", hook)
    server = await aiohttp_server(app)

    raw = make_raw_config(tmp_path, notify_url=str(server.make_url("/hook")))
    env = await build_env(tmp_path, raw)
    try:
        inv = await env.payments.create_invoice(env.shop, method="stars", user_id=BUYER_ID, amount=7, title="Донат", payload="order-42")
        await env.payments.process_stars_payment(env.shop, payload=inv.tg_payload, charge_id="c", total_amount=7, currency="XTR", user_id=BUYER_ID)
        await env.notifier.wait_idle()
    finally:
        await env.notifier.close(grace=1)
        await env.db.close()

    [(body, signature)] = received
    assert signature == hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
    event = json.loads(body)
    assert event["event"] == "invoice_paid"
    assert event["invoice"]["payload"] == "order-42" and event["invoice"]["amount"] == "7"
    assert event["bot"]["name"] == "shop" and event["user"]["id"] == BUYER_ID
    # Для товара без product_id покупатель получает стандартное «оплата получена».
    assert messages_to(env.session(), BUYER_ID) == [env.shop.cfg.text("paid")]
