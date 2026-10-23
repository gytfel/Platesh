from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal

import pytest
from aiohttp import web

from platesh.cryptopay import CryptoPayClient, CryptoPayError, invoice_url, verify_signature


def test_verify_signature():
    token, body = "123:abc", b'{"update_type":"invoice_paid"}'
    good = hmac.new(hashlib.sha256(token.encode()).digest(), body, hashlib.sha256).hexdigest()
    assert verify_signature(token, body, good)
    assert verify_signature(token, body, good.upper())
    assert not verify_signature(token, body + b" ", good)
    assert not verify_signature("other", body, good)
    assert not verify_signature(token, body, None)


def test_invoice_url_prefers_new_field():
    assert invoice_url({"pay_url": "old", "bot_invoice_url": "new"}) == "new"
    assert invoice_url({"pay_url": "old"}) == "old"


@pytest.fixture
async def fake_api(aiohttp_server):
    calls = []

    async def handler(request):
        method = request.match_info["method"]
        body = await request.json()
        calls.append((method, body, request.headers.get("Crypto-Pay-API-Token")))
        if method == "createInvoice":
            if body["amount"] == "0.0001":
                return web.json_response({"ok": False, "error": {"code": 400, "name": "AMOUNT_TOO_SMALL"}})
            return web.json_response({"ok": True, "result": {"invoice_id": 1, "status": "active", "bot_invoice_url": "u"}})
        if method == "getInvoices":
            ids = body["invoice_ids"].split(",")
            return web.json_response({"ok": True, "result": {"items": [{"invoice_id": int(i), "status": "active"} for i in ids]}})
        return web.json_response({"ok": False, "error": {"code": 405, "name": "METHOD_NOT_FOUND"}}, status=405)

    app = web.Application()
    app.router.add_post("/api/{method}", handler)
    server = await aiohttp_server(app)
    client = CryptoPayClient("tok", base_url=str(server.make_url("/api")))
    yield client, calls
    await client.close()


async def test_create_invoice_fiat(fake_api):
    client, calls = fake_api
    result = await client.create_invoice(
        Decimal("4.990"), currency="USD", is_fiat=True, accepted_assets=("USDT", "TON"),
        description="VIP", payload="pl:abc", expires_in=600, paid_btn_url="https://t.me/bot",
    )
    assert result["invoice_id"] == 1
    method, body, token = calls[0]
    assert method == "createInvoice" and token == "tok"
    assert body == {
        "amount": "4.99", "description": "VIP", "payload": "pl:abc", "expires_in": 600, "allow_comments": False,
        "currency_type": "fiat", "fiat": "USD", "accepted_assets": "USDT,TON",
        "paid_btn_name": "openBot", "paid_btn_url": "https://t.me/bot",
    }


async def test_create_invoice_crypto_asset(fake_api):
    client, calls = fake_api
    await client.create_invoice(Decimal(10), currency="TON", is_fiat=False)
    body = calls[0][1]
    assert body["currency_type"] == "crypto" and body["asset"] == "TON" and body["amount"] == "10"
    assert "fiat" not in body and "paid_btn_name" not in body


async def test_errors_raise(fake_api):
    client, _ = fake_api
    with pytest.raises(CryptoPayError) as err:
        await client.create_invoice(Decimal("0.0001"), currency="USD", is_fiat=True)
    assert err.value.name == "AMOUNT_TOO_SMALL" and err.value.code == 400
    with pytest.raises(CryptoPayError):
        await client.call("unknownMethod")


async def test_get_invoices_chunks(fake_api):
    client, calls = fake_api
    items = await client.get_invoices(list(range(1, 251)))
    assert len(items) == 250
    assert [len(c[1]["invoice_ids"].split(",")) for c in calls] == [100, 100, 50]
