"""Прокси: системный (VPN-клиент в Windows выставляет «системный прокси»), ручной, выключенный."""

from __future__ import annotations

import pytest

from platesh import net
from platesh.config import parse_config

from .conftest import make_raw_config


@pytest.mark.parametrize("system, expected", [
    ({}, None),
    ({"http": "http://127.0.0.1:10809", "https": "https://127.0.0.1:10809"}, "http://127.0.0.1:10809"),
    ({"socks": "socks://127.0.0.1:10808"}, "socks5://127.0.0.1:10808"),
    ({"https": "http://user:pw@proxy:3128"}, "http://user:pw@proxy:3128"),
])
def test_auto_uses_system_proxy(monkeypatch, system, expected):
    monkeypatch.setattr(net.urllib.request, "getproxies", lambda: system)
    assert net.resolve_proxy(None) == expected
    assert net.resolve_proxy("auto") == expected


def test_manual_and_off(monkeypatch):
    monkeypatch.setattr(net.urllib.request, "getproxies", lambda: {"http": "http://sys:1"})
    assert net.resolve_proxy("off") is None
    assert net.resolve_proxy("127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert net.resolve_proxy("SOCKS5H://127.0.0.1:10808") == "socks5://127.0.0.1:10808"


def test_mask_hides_credentials():
    assert net.mask_proxy("socks5://user:secret@10.0.0.1:1080") == "socks5://10.0.0.1:1080"


def test_config_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(net.urllib.request, "getproxies", lambda: {})
    raw = make_raw_config(tmp_path, proxy="socks5://127.0.0.1:10808")
    assert parse_config(raw, tmp_path).proxy_url == "socks5://127.0.0.1:10808"
    assert parse_config(make_raw_config(tmp_path), tmp_path).proxy_url is None


async def test_clients_use_proxy_connector():
    from aiohttp_socks import ProxyConnector

    from platesh.bots.registry import create_bot
    from platesh.cryptopay import CryptoPayClient

    client = CryptoPayClient("t", proxy="socks5://127.0.0.1:10808")
    session = await client._get_session()
    assert isinstance(session.connector, ProxyConnector)
    await client.close()

    bot = create_bot("123:abc", "socks5://127.0.0.1:10808")
    session = await bot.session.create_session()
    assert isinstance(session.connector, ProxyConnector)
    await bot.session.close()


async def test_refusing_proxy_means_no_connection_not_crash():
    """Прокси отказал (403) или не запущен — это «нет связи», а не падение с трассировкой."""
    import asyncio

    from aiogram.exceptions import TelegramNetworkError
    from aiohttp import web

    from platesh.bots.registry import create_bot
    from platesh.cryptopay import CryptoPayClient, CryptoPayError

    async def refuse(request):
        return web.Response(status=403, text="Forbidden")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", refuse)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        for proxy in (f"http://127.0.0.1:{port}", "socks5://127.0.0.1:1"):  # отказ и «никто не слушает»
            bot = create_bot("123:abc", proxy)
            try:
                with pytest.raises(TelegramNetworkError):
                    await asyncio.wait_for(bot.get_me(), 15)
            finally:
                await bot.session.close()
            client = CryptoPayClient("t", proxy=proxy)
            try:
                with pytest.raises(CryptoPayError, match="нет связи"):
                    await client.get_me()
            finally:
                await client.close()
    finally:
        await runner.cleanup()
