from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from aiogram.methods import RefundStarPayment, SendInvoice

from platesh.models import PAID
from platesh.web import create_app

from .conftest import BUYER_ID, CRYPTO_TOKEN, build_env, make_raw_config

AUTH = {"Authorization": "Bearer secret-key"}


@pytest.fixture
async def client(env, aiohttp_client):
    return await aiohttp_client(create_app(env.cfg, env.registry, env.payments))


async def test_auth(client):
    assert (await client.get("/health")).status == 200
    resp = await client.get("/api/v1/bots")
    assert resp.status == 401
    resp = await client.get("/api/v1/bots", headers={"X-API-Key": "secret-key"})
    assert resp.status == 200
    bots = (await resp.json())["result"]
    assert [b["name"] for b in bots] == ["shop", "ext"]
    assert bots[0]["products"][0]["price_crypto"] == "4.99"


async def test_api_disabled_without_key(tmp_path, aiohttp_client):
    raw = make_raw_config(tmp_path)
    raw["server"] = {}
    env = await build_env(tmp_path, raw)
    try:
        client = await aiohttp_client(create_app(env.cfg, env.registry, env.payments))
        assert (await client.get("/api/v1/bots", headers=AUTH)).status == 403
    finally:
        await env.db.close()


async def test_create_and_get_invoice(client, env):
    resp = await client.post("/api/v1/invoices", headers=AUTH, json={
        "bot": "shop", "method": "stars", "product_id": "vip", "user_id": BUYER_ID, "payload": "order-1",
    })
    assert resp.status == 200, await resp.text()
    inv = (await resp.json())["result"]
    assert inv["amount"] == "100" and inv["currency"] == "XTR" and inv["pay_url"] and inv["payload"] == "order-1"

    resp = await client.get(f"/api/v1/invoices/{inv['id']}", headers=AUTH)
    assert (await resp.json())["result"]["status"] == "pending"
    resp = await client.get("/api/v1/invoices", headers=AUTH, params={"bot": "shop", "user_id": str(BUYER_ID)})
    assert [i["id"] for i in (await resp.json())["result"]] == [inv["id"]]
    assert (await client.get("/api/v1/invoices/nope", headers=AUTH)).status == 404


async def test_create_custom_crypto_invoice_and_send(client, env):
    resp = await client.post("/api/v1/invoices", headers=AUTH, json={
        "bot": "ext", "method": "crypto", "amount": "12.5", "currency": "usdt", "title": "Подписка", "user_id": BUYER_ID, "send": True,
    })
    assert resp.status == 200, await resp.text()
    inv = (await resp.json())["result"]
    assert inv["currency"] == "USDT" and inv["amount"] == "12.5" and inv["pay_url"]
    crypto = env.payments.crypto(env.ext)
    assert crypto.created[-1]["is_fiat"] is False and crypto.created[-1]["currency"] == "USDT"
    # У внешнего бота нет кнопки «Проверить оплату» — её некому обработать.
    msg = env.session("ext").requests[-1]
    assert [b.url for row in msg.reply_markup.inline_keyboard for b in row] == [inv["pay_url"]]


async def test_crypto_invoice_in_chosen_coin(client, env):
    resp = await client.get("/api/v1/bots", headers=AUTH)
    assert {b["name"]: b["crypto_assets"] for b in (await resp.json())["result"]}["ext"] == ["USDT", "TON"]

    resp = await client.post("/api/v1/invoices", headers=AUTH, json={
        "bot": "ext", "method": "crypto", "amount": "5", "title": "Подписка", "asset": "ton", "user_id": BUYER_ID, "send": True,
    })
    assert resp.status == 200, await resp.text()
    created = env.payments.crypto(env.ext).created[-1]
    assert created["is_fiat"] is True and created["currency"] == "USD" and created["accepted_assets"] == ("TON",)
    assert "Оплата в <b>TON</b>" in env.session("ext").requests[-1].text

    resp = await client.post("/api/v1/invoices", headers=AUTH, json={
        "bot": "ext", "method": "crypto", "amount": "5", "title": "Подписка", "asset": "SOL"})
    assert resp.status == 400 and "accepted_assets" in (await resp.json())["error"]


@pytest.mark.parametrize("body, status", [
    ({"bot": "nope", "method": "stars", "amount": 5, "title": "x"}, 404),
    ({"bot": "shop", "method": "card", "amount": 5, "title": "x"}, 400),
    ({"bot": "shop", "method": "stars", "amount": 1.5, "title": "x"}, 400),
    ({"bot": "shop", "method": "stars", "amount": 5}, 400),
    ({"bot": "shop", "method": "stars", "product_id": "missing"}, 404),
    ({"bot": "shop", "method": "crypto", "product_id": "stars_only"}, 400),
    ({"bot": "shop", "method": "stars", "amount": 5, "title": "x", "send": True}, 400),
    ({"bot": "shop", "method": "stars", "amount": 5, "title": "x", "user_id": "abc"}, 400),
])
async def test_create_invoice_validation(client, body, status):
    resp = await client.post("/api/v1/invoices", headers=AUTH, json=body)
    assert resp.status == status
    assert (await resp.json())["ok"] is False


async def test_bad_json(client):
    resp = await client.post("/api/v1/invoices", headers=AUTH, data="not json")
    assert resp.status == 400


async def test_external_bot_stars_flow(client, env):
    resp = await client.post("/api/v1/invoices", headers=AUTH, json={
        "bot": "ext", "method": "stars", "amount": 25, "title": "Донат", "user_id": BUYER_ID, "send": True,
    })
    inv = (await resp.json())["result"]
    assert env.session("ext").calls(SendInvoice)[0].payload == f"pl:{inv['id']}"

    check = {"bot": "ext", "payload": f"pl:{inv['id']}", "currency": "XTR", "total_amount": 25}
    result = (await (await client.post("/api/v1/stars/pre-checkout", headers=AUTH, json=check)).json())["result"]
    assert result == {"accept": True, "error": None}
    bad = dict(check, total_amount=1)
    result = (await (await client.post("/api/v1/stars/pre-checkout", headers=AUTH, json=bad)).json())["result"]
    assert result["accept"] is False and result["error"]

    paid = dict(check, charge_id="tg-charge", user_id=BUYER_ID, username="buyer")
    resp = await client.post("/api/v1/stars/paid", headers=AUTH, json=paid)
    assert (await resp.json())["result"]["status"] == PAID
    assert (await env.db.get_user("ext", BUYER_ID))["username"] == "buyer"

    resp = await client.post(f"/api/v1/invoices/{inv['id']}/refund", headers=AUTH)
    assert (await resp.json())["result"]["status"] == "refunded"
    assert env.session("ext").calls(RefundStarPayment)[0].telegram_payment_charge_id == "tg-charge"


async def test_cryptobot_webhook_endpoint(client, env):
    resp = await client.post("/api/v1/invoices", headers=AUTH, json={
        "bot": "shop", "method": "crypto", "product_id": "vip", "user_id": BUYER_ID,
    })
    inv = (await resp.json())["result"]
    item = env.crypto().set_status(inv["external_id"], "paid", paid_asset="USDT", paid_amount="4.99")
    body = json.dumps({"update_id": 7, "update_type": "invoice_paid", "payload": item}).encode()

    resp = await client.post("/webhooks/cryptobot", data=body, headers={"crypto-pay-api-signature": "00"})
    assert resp.status == 401
    signature = hmac.new(hashlib.sha256(CRYPTO_TOKEN.encode()).digest(), body, hashlib.sha256).hexdigest()
    resp = await client.post("/webhooks/cryptobot", data=body, headers={"crypto-pay-api-signature": signature})
    assert resp.status == 200
    assert (await env.db.get_invoice(inv["id"])).status == PAID


# ---------------------------------------------------------------------- ключи ботов, IP, доступы

EXT_KEY = {"Authorization": "Bearer ext-bot-key"}


@pytest.fixture
async def scoped(tmp_path, aiohttp_client):
    raw = make_raw_config(tmp_path)
    raw["bots"][1]["api_key"] = "ext-bot-key"
    raw["bots"][1]["products"] = [{"id": "premium", "title": "Премиум", "price": {"stars": 10}, "days": 30}]
    env = await build_env(tmp_path, raw)
    client = await aiohttp_client(create_app(env.cfg, env.registry, env.payments))
    yield env, client
    await env.notifier.close(grace=1)
    await env.db.close()


async def test_bot_key_sees_only_its_bot(scoped):
    env, client = scoped
    bots = (await (await client.get("/api/v1/bots", headers=EXT_KEY)).json())["result"]
    assert [b["name"] for b in bots] == ["ext"] and bots[0]["products"][0]["days"] == 30

    resp = await client.post("/api/v1/invoices", headers=EXT_KEY, json={"bot": "shop", "method": "stars", "product_id": "vip"})
    assert resp.status == 403

    shop_inv = (await (await client.post("/api/v1/invoices", headers=AUTH, json={
        "bot": "shop", "method": "stars", "product_id": "vip", "user_id": BUYER_ID})).json())["result"]
    assert (await client.get(f"/api/v1/invoices/{shop_inv['id']}", headers=EXT_KEY)).status == 404
    listed = (await (await client.get("/api/v1/invoices", headers=EXT_KEY, params={"bot": "shop"})).json())["result"]
    assert listed == []  # фильтр по чужому боту игнорируется — видны только свои


async def test_bot_key_cannot_refund_or_grant(scoped):
    env, client = scoped
    inv = (await (await client.post("/api/v1/invoices", headers=EXT_KEY, json={
        "bot": "ext", "method": "stars", "product_id": "premium", "user_id": BUYER_ID})).json())["result"]
    await client.post("/api/v1/stars/paid", headers=EXT_KEY, json={
        "bot": "ext", "payload": f"pl:{inv['id']}", "charge_id": "c1", "total_amount": 10, "currency": "XTR", "user_id": BUYER_ID})
    assert (await client.post(f"/api/v1/invoices/{inv['id']}/refund", headers=EXT_KEY)).status == 403
    grant = {"bot": "ext", "user_id": 1, "product_id": "premium"}
    assert (await client.post("/api/v1/access/grant", headers=EXT_KEY, json=grant)).status == 403
    assert (await client.post("/api/v1/access/grant", headers=AUTH, json=grant)).status == 200


async def test_managed_bot_payments_cannot_be_confirmed_via_api(scoped):
    env, client = scoped
    inv = (await (await client.post("/api/v1/invoices", headers=AUTH, json={
        "bot": "shop", "method": "stars", "product_id": "vip", "user_id": BUYER_ID})).json())["result"]
    fake = {"bot": "shop", "payload": f"pl:{inv['id']}", "charge_id": "forged", "total_amount": 100,
            "currency": "XTR", "user_id": BUYER_ID}
    resp = await client.post("/api/v1/stars/paid", headers=AUTH, json=fake)
    assert resp.status == 403
    assert (await env.db.get_invoice(inv["id"])).status == "pending"


async def test_access_endpoints(scoped):
    env, client = scoped
    params = {"bot": "ext", "user_id": str(BUYER_ID), "product_id": "premium"}
    assert (await (await client.get("/api/v1/access", headers=EXT_KEY, params=params)).json())["result"]["active"] is False

    inv = (await (await client.post("/api/v1/invoices", headers=EXT_KEY, json={
        "bot": "ext", "method": "stars", "product_id": "premium", "user_id": BUYER_ID})).json())["result"]
    await client.post("/api/v1/stars/paid", headers=EXT_KEY, json={
        "bot": "ext", "payload": f"pl:{inv['id']}", "charge_id": "c1", "total_amount": 10, "currency": "XTR", "user_id": BUYER_ID})
    result = (await (await client.get("/api/v1/access", headers=EXT_KEY, params=params)).json())["result"]
    assert result["active"] is True and result["accesses"][0]["expires_at"]

    resp = await client.post("/api/v1/access/revoke", headers=AUTH, json={"bot": "ext", "user_id": BUYER_ID, "product_id": "premium"})
    assert (await resp.json())["result"]["revoked"] is True
    assert (await (await client.get("/api/v1/access", headers=EXT_KEY, params=params)).json())["result"]["active"] is False


async def test_allowed_ips(tmp_path, aiohttp_client):
    raw = make_raw_config(tmp_path)
    raw["server"]["allowed_ips"] = ["10.0.0.0/8"]
    env = await build_env(tmp_path, raw)
    try:
        client = await aiohttp_client(create_app(env.cfg, env.registry, env.payments))
        resp = await client.get("/api/v1/bots", headers=AUTH)
        assert resp.status == 403 and "IP" in (await resp.json())["error"]
        assert (await client.get("/health")).status == 200
    finally:
        await env.db.close()

    raw["server"]["allowed_ips"] = ["127.0.0.1", "::1"]
    env = await build_env(tmp_path, raw)
    try:
        client = await aiohttp_client(create_app(env.cfg, env.registry, env.payments))
        assert (await client.get("/api/v1/bots", headers=AUTH)).status == 200
    finally:
        await env.db.close()
