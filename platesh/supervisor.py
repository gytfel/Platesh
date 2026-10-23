"""Запуск кода external-ботов вместе с Platesh: один терминал, автоперезапуск при падении."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from .config import Config

if TYPE_CHECKING:
    from .bots.registry import BotRuntime

log = logging.getLogger("platesh.bots")

STOP_TIMEOUT = 10
MAX_RESTART_DELAY = 60
STABLE_AFTER = 60  # проработал столько секунд — считаем, что упал случайно, и перезапускаем быстро


def bot_env(cfg: Config, rt: BotRuntime) -> dict[str, str]:
    """Переменные, по которым код бота подключается к Platesh: PlateshClient.from_env()."""
    host = cfg.server.host if cfg.server.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    env = dict(os.environ)
    env.update({
        "PLATESH_URL": f"http://{host}:{cfg.server.port}",
        "PLATESH_BOT": rt.name,
        "PLATESH_ADMINS": ",".join(str(a) for a in rt.cfg.admins),
        "PYTHONUNBUFFERED": "1",  # чтобы вывод бота сразу появлялся в терминале
        "PYTHONIOENCODING": "utf-8",
    })
    if rt.cfg.api_key:
        env["PLATESH_KEY"] = rt.cfg.api_key
    return env


class BotProcess:
    def __init__(self, name: str, command: tuple[str, ...], cwd: str | None, env: dict[str, str],
                 *, first_delay: float = 1.0) -> None:
        self.name = name
        self.command = command
        self.cwd = cwd
        self.env = env
        self.first_delay = first_delay
        self.restarts = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever(), name=f"bot-{self.name}")

    async def _pipe_output(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue  # слишком длинная строка — пропускаем
            if not line:
                return
            log.info("[%s] %s", self.name, line.decode("utf-8", errors="replace").rstrip())

    async def _run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        delay = self.first_delay
        try:
            while True:
                started = loop.time()
                try:
                    self._proc = await asyncio.create_subprocess_exec(
                        *self.command, cwd=self.cwd, env=self.env,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=1024 * 1024,
                    )
                except OSError as exc:
                    log.error("[%s] не удалось запустить «%s»: %s", self.name, " ".join(self.command), exc)
                    code = None
                else:
                    log.info("[%s] запущен: %s (pid %s)", self.name, " ".join(self.command), self._proc.pid)
                    await self._pipe_output(self._proc)
                    code = await self._proc.wait()
                if loop.time() - started > STABLE_AFTER:
                    delay = self.first_delay
                log.warning("[%s] остановился (код %s), перезапуск через %.0f с", self.name, code, delay)
                await asyncio.sleep(delay)
                self.restarts += 1
                delay = min(delay * 2, MAX_RESTART_DELAY)
        finally:
            await self._terminate()

    async def _terminate(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        log.info("[%s] остановлен", self.name)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


def start_bot_processes(cfg: Config, runtimes: list[BotRuntime]) -> list[BotProcess]:
    processes = []
    for rt in runtimes:
        run = rt.cfg.run
        if run is None:
            continue
        if not cfg.server.enabled:
            log.warning("[%s] HTTP API выключен (server.enabled: false) — боту не к чему подключаться", rt.name)
        if not rt.cfg.api_key:
            log.warning("[%s] у бота нет api_key — PlateshClient.from_env() не сможет подключиться", rt.name)
        process = BotProcess(rt.name, run.command, str(run.cwd) if run.cwd else None, bot_env(cfg, rt))
        process.start()
        processes.append(process)
    return processes
