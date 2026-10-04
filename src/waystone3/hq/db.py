"""Small SQL helpers: rows are dicts, statements are built from their keys."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

Row = dict[str, Any]
Conn = psycopg.Connection[Row]
Cursor = psycopg.Cursor[Row]


def connect(dsn: str) -> Conn:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=False)


def _table(name: str) -> sql.Composed:
    schema, table = name.split(".", 1)
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def upsert(
    cur: Cursor,
    table: str,
    row: Mapping[str, Any],
    conflict: Sequence[str] = (),
    *,
    update: bool = True,
    returning: str | None = None,
) -> Row | None:
    """INSERT one row; on conflict update every non-key column (or do nothing)."""
    cols = list(row)
    stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        _table(table),
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder(c) for c in cols),
    )
    if conflict:
        target = sql.SQL(", ").join(map(sql.Identifier, conflict))
        changes = [c for c in cols if c not in conflict]
        if update and changes:
            sets = sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
                for c in changes
            )
            stmt += sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {}").format(target, sets)
        else:
            stmt += sql.SQL(" ON CONFLICT ({}) DO NOTHING").format(target)
    if returning:
        stmt += sql.SQL(" RETURNING {}").format(sql.Identifier(returning))
    cur.execute(stmt, row)
    return cur.fetchone() if returning else None


def copy_rows(cur: Cursor, table: str, cols: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        _table(table), sql.SQL(", ").join(map(sql.Identifier, cols))
    )
    with cur.copy(stmt) as copy:
        for values in rows:
            copy.write_row(values)


def delete_for_strategy(cur: Cursor, tables: Iterable[str], strategy_id: int) -> None:
    for table in tables:
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE strategy_id = %s").format(_table(table)), (strategy_id,)
        )
