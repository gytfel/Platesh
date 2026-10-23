from __future__ import annotations

from decimal import Decimal

import pytest

from platesh.config import ConfigError, load_config, parse_config

from .conftest import make_raw_config


def test_example_config_loads(monkeypatch, tmp_path):
    from pathlib import Path

    monkeypatch.setenv("SHOP_BOT_TOKEN", "1:x")
    monkeypatch.chdir(tmp_path)  # чтобы не подхватить чужой .env
    cfg = load_config(Path(__file__).parent.parent / "config.example.yaml")
    assert [b.name for b in cfg.bots] == ["shop"]  # остальные без токена пропускаются
    shop = cfg.bots[0]
    assert shop.products["vip"].price_crypto == Decimal("9.99")
    assert shop.products["vip"].delivery.invite_chat_id == -1001234567890


def test_env_substitution(monkeypatch, tmp_path):
    raw = make_raw_config(tmp_path)
    raw["bots"][0]["token"] = "${MY_TOKEN}"
    raw["cryptobot"]["token"] = "${NO_SUCH_VAR:-}"
    monkeypatch.setenv("MY_TOKEN", "42:abc")
    cfg = parse_config(raw, tmp_path)
    assert cfg.bots[0].token == "42:abc"
    assert cfg.bots[0].crypto.enabled is False
    assert cfg.bots[0].methods_for(cfg.bots[0].products["vip"]) == ["stars"]

    raw["bots"][0]["token"] = "${MISSING_TOKEN_VAR}"
    with pytest.raises(ConfigError, match="MISSING_TOKEN_VAR"):
        parse_config(raw, tmp_path)


def test_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("DOTENV_TOKEN", raising=False)
    (tmp_path / ".env").write_text('# comment\nDOTENV_TOKEN="77:from-dotenv"\n', encoding="utf-8")
    (tmp_path / "config.yaml").write_text("bots:\n  - name: a\n    token: ${DOTENV_TOKEN}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_config(tmp_path / "config.yaml").bots[0].token == "77:from-dotenv"


def test_inheritance_and_overrides(tmp_path):
    raw = make_raw_config(tmp_path)
    raw["bots"][1]["admins"] = [2000]
    raw["bots"][1]["cryptobot"] = {"currency": "ton", "expires_in": 600}
    raw["bots"][1]["texts"] = {"paid": "Готово!"}
    raw["bots"][1]["welcome"] = "Здарова"
    cfg = parse_config(raw, tmp_path)
    shop, ext = cfg.bots
    assert shop.admins == (1000,) and ext.admins == (1000, 2000)
    assert shop.crypto.currency == "USD" and shop.crypto.is_fiat
    assert ext.crypto.currency == "TON" and not ext.crypto.is_fiat and ext.crypto.expires_in == 600
    assert ext.crypto.token == shop.crypto.token
    assert ext.text("paid") == "Готово!" and ext.text("welcome") == "Здарова"
    assert shop.text("paid") != "Готово!"
    assert ext.notify_url == "http://example.invalid/hook" and shop.notify_url is None


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r.update(bots=[]), "хотя бы одного"),
    (lambda r: r["bots"][0].update(name="bad name!"), "name"),
    (lambda r: r["bots"][0].update(mode="weird"), "mode"),
    (lambda r: r["bots"][0].update(payments=["paypal"]), "payments"),
    (lambda r: r["bots"][1].update(name="shop"), "повторяется"),
    (lambda r: r["bots"][1].update(token=r["bots"][0]["token"]), "токен"),
    (lambda r: r["bots"][0]["products"][0].update(price={}), "хотя бы одну цену"),
    (lambda r: r["bots"][0]["products"][0].update(price={"stars": 1.5}), "целое"),
    (lambda r: r["bots"][0]["products"][0].update(price={"crypto": "-1"}), "больше нуля"),
    (lambda r: r["bots"][0]["products"][1].update(id="vip"), "повторяется"),
    (lambda r: r["cryptobot"].update(expires_in=10), "expires_in"),
])
def test_validation_errors(tmp_path, mutate, message):
    raw = make_raw_config(tmp_path)
    mutate(raw)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw, tmp_path)


def test_bot_without_token_is_skipped(tmp_path):
    raw = make_raw_config(tmp_path)
    raw["bots"][1]["token"] = ""
    assert [b.name for b in parse_config(raw, tmp_path).bots] == ["shop"]


def test_delivery_file_resolved_relative_to_config(tmp_path):
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "guide.pdf").write_bytes(b"%PDF")
    raw = make_raw_config(tmp_path)
    raw["bots"][0]["products"][1]["deliver"] = {"file": "files/guide.pdf"}
    product = parse_config(raw, tmp_path).bots[0].products["stars_only"]
    assert product.delivery.file == str(tmp_path / "files" / "guide.pdf")
