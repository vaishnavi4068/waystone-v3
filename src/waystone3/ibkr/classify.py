"""Map IBKR secType / clientId to a futures vs options book."""

from __future__ import annotations

from waystone3.ibkr.models import Book

_OPTIONS = {"OPT", "FOP"}


def parse_client_books(raw: str) -> dict[int, Book]:
    """Parse ``IBKR_CLIENT_BOOKS`` like ``1=futures,2=options``."""
    out: dict[int, Book] = {}
    for part in raw.split(","):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        cid_raw, book_raw = piece.split("=", 1)
        try:
            cid = int(cid_raw.strip())
        except ValueError:
            continue
        try:
            out[cid] = Book(book_raw.strip().lower())
        except ValueError:
            continue
    return out


def book_for(
    sec_type: str,
    client_id: int | None = None,
    client_books: dict[int, Book] | None = None,
) -> Book:
    if client_id is not None and client_books and client_id in client_books:
        return client_books[client_id]
    st = sec_type.strip().upper()
    if st == "FUT":
        return Book.FUTURES
    if st in _OPTIONS:
        return Book.OPTIONS
    return Book.OTHER
