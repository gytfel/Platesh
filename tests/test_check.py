"""python -m platesh check: какие монеты на самом деле принимает CryptoBot."""

from __future__ import annotations

from platesh import cryptopay
from platesh.__main__ import check_coins
from platesh.config import parse_config

from .conftest import make_raw_config


class ProbeCrypto:
    created: list[tuple] = []
    deleted: list[int] = []

    def __init__(self, token, *, testnet=False, proxy=None):
        pass

    async def create_invoice(self, amount, *, currency, is_fiat, accepted_assets, **kwargs):
        if accepted_assets == ("SOL",):
            raise cryptopay.CryptoPayError("createInvoice", 400, "ACCEPTED_ASSETS_INVALID")
        ProbeCrypto.created.append((amount, currency, is_fiat, accepted_assets))
        return {"invoice_id": 900 + len(ProbeCrypto.created)}

    async def delete_invoice(self, invoice_id):
        ProbeCrypto.deleted.append(invoice_id)

    async def close(self):
        pass


async def test_check_coins_reports_rejected_coin(tmp_path, monkeypatch):
    monkeypatch.setattr(cryptopay, "CryptoPayClient", ProbeCrypto)
    raw = make_raw_config(tmp_path)
    raw["cryptobot"]["accepted_assets"] = ["TON", "USDT", "SOL"]
    shop = parse_config(raw, tmp_path).bots[0]
    line, ok = await check_coins(shop, None)
    assert not ok
    assert line.startswith("✅ TON, ✅ USDT, ❌ Solana") and "ACCEPTED_ASSETS_INVALID" in line and "accepted_assets" in line
    # Пробные счета — на цену товара в USD, каждый сразу удалён.
    assert [c[3] for c in ProbeCrypto.created] == [("TON",), ("USDT",)] and ProbeCrypto.created[0][1:3] == ("USD", True)
    assert ProbeCrypto.deleted == [901, 902]
