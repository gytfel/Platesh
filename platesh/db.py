"""Хранилище счетов и покупателей (SQLite)."""

from __future__ import annotations

import time
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from .models import EXPIRED, PAID, PENDING, Invoice

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id          TEXT PRIMARY KEY,
    bot         TEXT NOT NULL,
    method      TEXT NOT NULL,
    amount      TEXT NOT NULL,
    currency    TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    user_id     INTEGER,
    product_id  TEXT,
    status      TEXT NOT NULL,
    pay_url     TEXT,
    external_id TEXT,
    charge_id   TEXT,
    paid_asset  TEXT,
    paid_amount TEXT,
    payload     TEXT,
    created_at  INTEGER NOT NULL,
    paid_at     INTEGER
);
CREATE INDEX IF NOT EXISTS invoices_status ON invoices (method, status);
CREATE INDEX IF NOT EXISTS invoices_user ON invoices (bot, user_id);
CREATE INDEX IF NOT EXISTS invoices_external ON invoices (method, external_id);

CREATE TABLE IF NOT EXISTS users (
    bot        TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    username   TEXT,
    first_name TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (bot, user_id)
);
"""

_COLUMNS = [f.name for f in fields(Invoice)]


def _row_to_invoice(row: aiosqlite.Row) -> Invoice:
    data = dict(row)
    data["amount"] = Decimal(data["amount"])
    return Invoice(**data)


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def open(cls, path: str | Path) -> Database:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def _fetch(self, sql: str, *args: Any) -> list[aiosqlite.Row]:
        async with self._conn.execute(sql, args) as cur:
            return list(await cur.fetchall())

    async def _write(self, sql: str, *args: Any) -> int:
        cur = await self._conn.execute(sql, args)
        await self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ invoices

    async def add_invoice(self, inv: Invoice) -> None:
        values = inv.to_dict()
        placeholders = ", ".join("?" for _ in _COLUMNS)
        await self._write(
            f"INSERT INTO invoices ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
            *(values[c] for c in _COLUMNS),
        )

    async def get_invoice(self, invoice_id: str) -> Invoice | None:
        rows = await self._fetch("SELECT * FROM invoices WHERE id = ?", invoice_id)
        return _row_to_invoice(rows[0]) if rows else None

    async def find_by_external(self, method: str, external_id: str) -> Invoice | None:
        rows = await self._fetch(
            "SELECT * FROM invoices WHERE method = ? AND external_id = ?", method, external_id
        )
        return _row_to_invoice(rows[0]) if rows else None

    async def find_reusable(self, bot: str, user_id: int, product_id: str, method: str, since: int) -> Invoice | None:
        """Неоплаченный счёт на тот же товар, чтобы не плодить новые при повторном нажатии."""
        rows = await self._fetch(
            "SELECT * FROM invoices WHERE bot = ? AND user_id = ? AND product_id = ? AND method = ?"
            " AND status = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            bot, user_id, product_id, method, PENDING, since,
        )
        return _row_to_invoice(rows[0]) if rows else None

    async def set_payment_link(self, invoice_id: str, pay_url: str | None, external_id: str | None) -> None:
        await self._write(
            "UPDATE invoices SET pay_url = ?, external_id = ? WHERE id = ?", pay_url, external_id, invoice_id
        )

    async def mark_paid(
        self,
        invoice_id: str,
        *,
        charge_id: str | None,
        paid_asset: str | None = None,
        paid_amount: str | None = None,
        user_id: int | None = None,
    ) -> bool:
        """Переводит счёт в «оплачен». Возвращает True только для первого успешного вызова."""
        changed = await self._write(
            "UPDATE invoices SET status = ?, charge_id = ?, paid_asset = ?, paid_amount = ?, paid_at = ?,"
            " user_id = COALESCE(user_id, ?) WHERE id = ? AND status IN (?, ?)",
            PAID, charge_id, paid_asset, paid_amount, int(time.time()), user_id, invoice_id, PENDING, EXPIRED,
        )
        return changed > 0

    async def set_status(self, invoice_id: str, status: str, *, expected: str) -> bool:
        changed = await self._write(
            "UPDATE invoices SET status = ? WHERE id = ? AND status = ?", status, invoice_id, expected
        )
        return changed > 0

    async def pending_crypto(self) -> list[Invoice]:
        rows = await self._fetch(
            "SELECT * FROM invoices WHERE method = 'crypto' AND status = ? AND external_id IS NOT NULL"
            " ORDER BY created_at",
            PENDING,
        )
        return [_row_to_invoice(r) for r in rows]

    async def list_invoices(
        self,
        *,
        bot: str | None = None,
        user_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Invoice]:
        where, args = [], []
        for column, value in (("bot", bot), ("user_id", user_id), ("status", status)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        sql = "SELECT * FROM invoices"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(paid_at, created_at) DESC LIMIT ?"
        rows = await self._fetch(sql, *args, limit)
        return [_row_to_invoice(r) for r in rows]

    async def revenue(self, *, bot: str | None = None, since: int | None = None) -> list[tuple[str, int, Decimal]]:
        """[(валюта, количество оплат, сумма)] по оплаченным счетам."""
        sql = "SELECT currency, amount FROM invoices WHERE status = ?"
        args: list[Any] = [PAID]
        if bot is not None:
            sql += " AND bot = ?"
            args.append(bot)
        if since is not None:
            sql += " AND paid_at >= ?"
            args.append(since)
        totals: dict[str, tuple[int, Decimal]] = {}
        for row in await self._fetch(sql, *args):
            count, total = totals.get(row["currency"], (0, Decimal(0)))
            totals[row["currency"]] = (count + 1, total + Decimal(row["amount"]))
        return [(cur, count, total) for cur, (count, total) in sorted(totals.items())]

    # ------------------------------------------------------------------ users

    async def upsert_user(self, bot: str, user_id: int, username: str | None, first_name: str | None) -> None:
        await self._write(
            "INSERT INTO users (bot, user_id, username, first_name, created_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (bot, user_id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
            bot, user_id, username, first_name, int(time.time()),
        )

    async def get_user(self, bot: str, user_id: int) -> dict[str, Any] | None:
        rows = await self._fetch("SELECT * FROM users WHERE bot = ? AND user_id = ?", bot, user_id)
        return dict(rows[0]) if rows else None

    async def count_users(self, bot: str | None = None) -> int:
        if bot is None:
            rows = await self._fetch("SELECT COUNT(*) FROM users")
        else:
            rows = await self._fetch("SELECT COUNT(*) FROM users WHERE bot = ?", bot)
        return int(rows[0][0])
