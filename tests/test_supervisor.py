"""Запуск кода external-ботов из Platesh: вывод в общий терминал, перезапуск, остановка."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from platesh.supervisor import BotProcess, bot_env, start_bot_processes

from .conftest import build_env, make_raw_config


async def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("не дождались")
        await asyncio.sleep(0.05)


async def test_output_is_logged_and_crashed_bot_restarts(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="platesh.bots")
    script = "import os; print('бот', os.environ['PLATESH_BOT'], 'ключ', os.environ['PLATESH_KEY']); raise SystemExit(3)"
    proc = BotProcess("bot1", (sys.executable, "-c", script), str(tmp_path),
                      {"PLATESH_BOT": "bot1", "PLATESH_KEY": "k"}, first_delay=0.05)
    proc.start()
    await wait_for(lambda: proc.restarts >= 2)
    await proc.stop()
    messages = [r.getMessage() for r in caplog.records]
    assert any("[bot1] бот bot1 ключ k" in m for m in messages)
    assert any("остановился (код 3)" in m for m in messages)


async def test_stop_terminates_running_bot(tmp_path):
    marker = tmp_path / "started"
    script = f"import pathlib, time; pathlib.Path({str(marker)!r}).write_text('1'); time.sleep(60)"
    proc = BotProcess("bot1", (sys.executable, "-c", script), None, {}, first_delay=0.05)
    proc.start()
    await wait_for(marker.exists)
    started = time.monotonic()
    await proc.stop()
    assert time.monotonic() - started < 5
    assert proc._proc.returncode is not None


async def test_missing_program_is_reported_not_crashing(tmp_path, caplog):
    proc = BotProcess("bot1", ("no-such-program-xyz",), None, {}, first_delay=0.05)
    proc.start()
    await wait_for(lambda: proc.restarts >= 1)
    await proc.stop()
    assert any("не удалось запустить" in r.getMessage() for r in caplog.records)


async def test_env_and_start_from_config(tmp_path):
    raw = make_raw_config(tmp_path)
    raw["bots"][1]["api_key"] = "ext-key"
    raw["bots"][1]["run"] = {"command": [sys.executable, "-c", "import time; time.sleep(60)"], "cwd": str(tmp_path)}
    env = await build_env(tmp_path, raw)
    try:
        values = bot_env(env.cfg, env.ext)
        assert values["PLATESH_URL"] == "http://127.0.0.1:8080"
        assert values["PLATESH_KEY"] == "ext-key" and values["PLATESH_BOT"] == "ext" and values["PLATESH_ADMINS"] == "1000"
        processes = start_bot_processes(env.cfg, env.registry.all())
        assert [p.name for p in processes] == ["ext"]  # managed-бота запускает сам Platesh, не отдельный процесс
        await asyncio.gather(*(p.stop() for p in processes))
    finally:
        await env.db.close()
