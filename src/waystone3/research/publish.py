"""Upload local wsbt results/ into dated GCS keys the dashboard can list."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

from waystone3.ibkr.store import ReportStore, build_report_store_from_env
from waystone3.ibkr.timeutil import NY
from waystone3.research.catalog import get_strategy, list_strategies, load_catalog
from waystone3.research.paths import (
    CATALOG_KEY,
    equity_key,
    latest_key,
    manifest_key,
    metrics_key,
    scorecard_html_key,
    scorecard_key,
    scorecards_index_key,
    scorecards_latest_index_key,
    success_key,
    toolkit_root,
    trades_key,
)
from waystone3.research.scorecard import (
    build_scorecard,
    render_index_html,
    render_scorecard_html,
    write_local_scorecard,
)
from waystone3.research.window import years_from_equity


def _git_sha(root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True)
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _results_dir() -> Path:
    env = os.getenv("WSBT_RESULTS_DIR", "").strip()
    if env:
        return Path(env)
    return toolkit_root() / "results"


def result_folders(results: Path, strategy_id: str) -> list[Path]:
    if not results.is_dir():
        return []
    found = [
        path
        for path in results.iterdir()
        if path.is_dir()
        and path.name.startswith(strategy_id)
        and (path / "metrics.json").is_file()
    ]
    return sorted(found, key=lambda p: p.stat().st_mtime)


def as_of_from_equity(path: Path, fallback: date) -> date:
    if not path.is_file():
        return fallback
    last: date | None = None
    for line in path.read_text().splitlines()[1:]:
        raw = line.split(",", 1)[0].strip()
        if not raw:
            continue
        try:
            last = date.fromisoformat(raw[:10])
        except ValueError:
            continue
    return last or fallback


def variant_name(strategy_id: str, folder: Path) -> str:
    name = folder.name
    if name == strategy_id:
        return "default"
    prefix = f"{strategy_id}_"
    return name[len(prefix) :] if name.startswith(prefix) else name


def publish_results(
    *,
    store: ReportStore | None = None,
    run_id: str | None = None,
    synthetic: bool = False,
    host: str | None = None,
    as_of: date | None = None,
) -> list[dict[str, str]]:
    reports = store or build_report_store_from_env()
    if reports is None:
        raise ValueError("set IBKR_REPORTS_BUCKET or IBKR_REPORTS_LOCAL_DIR")
    rid = run_id or datetime.now(NY).strftime("%Y%m%dT%H%M%S")
    published: list[dict[str, str]] = []
    cards: list[dict[str, Any]] = []
    reports.put(CATALOG_KEY, json.dumps(load_catalog(), indent=2).encode(), "application/json")
    root = toolkit_root()
    repo = root.parent if root.name == "waystone_backtests" else root
    results = _results_dir()
    today = as_of or datetime.now(NY).date()
    for row in list_strategies():
        sid = str(row["id"])
        folders = result_folders(results, sid)
        if not folders:
            continue
        latest_day = today
        latest_variant = "default"
        for folder in folders:
            variant = variant_name(sid, folder)
            day = as_of_from_equity(folder / "equity.csv", today)
            reports.put(
                metrics_key(sid, day, variant),
                (folder / "metrics.json").read_bytes(),
                "application/json",
            )
            if (folder / "equity.csv").is_file():
                reports.put(
                    equity_key(sid, day, variant),
                    (folder / "equity.csv").read_bytes(),
                    "text/csv",
                )
            if (folder / "trades.csv").is_file():
                reports.put(
                    trades_key(sid, day, variant),
                    (folder / "trades.csv").read_bytes(),
                    "text/csv",
                )
            metrics = json.loads((folder / "metrics.json").read_text())
            catalog = get_strategy(sid) or {"id": sid, "name": sid}
            card = build_scorecard(
                strategy=catalog,
                variant=variant,
                day=day.isoformat(),
                metrics=metrics if isinstance(metrics, dict) else {},
                equity_csv=(folder / "equity.csv").read_text()
                if (folder / "equity.csv").is_file()
                else "",
                trades_csv=(folder / "trades.csv").read_text()
                if (folder / "trades.csv").is_file()
                else "",
            )
            write_local_scorecard(folder, card)
            reports.put(
                scorecard_key(sid, day, variant),
                json.dumps(card, indent=2, default=str).encode(),
                "application/json",
            )
            reports.put(
                scorecard_html_key(sid, day, variant),
                render_scorecard_html(card).encode(),
                "text/html",
            )
            payload: dict[str, Any] = {
                "strategy_id": sid,
                "variant": variant,
                "date": day.isoformat(),
                "run_id": rid,
                "synthetic": synthetic,
                "host": host or socket.gethostname(),
                "git_sha": _git_sha(repo),
                "published_at": datetime.now(NY).isoformat(),
                "window_years": years_from_equity(folder / "equity.csv") or 0,
            }
            reports.put(
                manifest_key(sid, day, variant),
                json.dumps(payload, indent=2).encode(),
                "application/json",
            )
            reports.put(success_key(sid, day, variant), b"ok\n", "text/plain")
            latest_day, latest_variant = day, variant
            cards.append(card)
            published.append(
                {
                    "id": sid,
                    "date": day.isoformat(),
                    "variant": variant,
                    "overall": str(card.get("overall") or ""),
                }
            )
        reports.put(
            latest_key(sid),
            json.dumps(
                {
                    "date": latest_day.isoformat(),
                    "variant": latest_variant,
                    "run_id": rid,
                    "synthetic": synthetic,
                }
            ).encode(),
            "application/json",
        )
    if cards:
        index_day = max(str(c.get("date") or today.isoformat()) for c in cards)
        index_html = render_index_html(cards, day=index_day).encode()
        reports.put(scorecards_index_key(index_day), index_html, "text/html")
        reports.put(scorecards_latest_index_key(), index_html, "text/html")
    return published
