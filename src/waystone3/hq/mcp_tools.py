"""MCP tools over the HQ database views (futures paper vs backtest, P&L, workbook KPIs)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from waystone3.hq.backtest_sandbox import HqBacktestSandbox
from waystone3.hq.context_lm import HqContextLanguageModel
from waystone3.hq.reader import HqReader
from waystone3.hq.strategy_runner import HqStrategyRunner


def _day(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD") from exc


def register_hq_strategy_exec_tools(
    mcp: FastMCP,
    runner: HqStrategyRunner,
    authorize: Callable[[], None],
    reader: HqReader | None = None,
    context_lm: HqContextLanguageModel | None = None,
    sandbox: HqBacktestSandbox | None = None,
) -> None:
    """Add ``hq_strategy_*``, ``hq_context_*``, ``hq_sandbox_*``, and ``ibkr_futures_*``
    tools for running ``hq-strategy`` algorithms and sandbox backtests on IBKR agentic
    paper accounts with immutable thresholds."""
    clm = (
        context_lm
        if context_lm is not None
        else HqContextLanguageModel(runner=runner, reader=reader)
    )
    sbx = sandbox if sandbox is not None else HqBacktestSandbox(runner=runner)

    @mcp.tool()
    def hq_strategy_runtime(strategy: str | None = None) -> dict[str, Any]:
        """Live runtime state of HQ futures strategies (nq_v221, es_v221, r2_mnq):
        bound IBKR paper account, client_id, contracts, process status, KILL_SWITCH state,
        locked engine thresholds, latest Renko/MCO engine state, and open SQLite trades."""
        authorize()
        return runner.status(strategy)

    @mcp.tool()
    def hq_strategy_thresholds(strategy: str | None = None) -> dict[str, Any]:
        """Return the immutable, validated signal and risk thresholds for HQ strategies
        (EMA periods, brick bounds, volume spike, FnG/chop gate, profit-take levels,
        hard stop %, and IBKR protective stop %). These thresholds are locked."""
        authorize()
        if strategy:
            spec = runner.spec(strategy)
            return {
                "strategy_code": spec.code,
                "thresholds_locked": True,
                "thresholds": spec.thresholds.to_dict(),
            }
        return {
            "thresholds_locked": True,
            "strategies": {
                code: runner.spec(code).thresholds.to_dict()
                for code in ("nq_v221", "es_v221", "r2_mnq")
            },
        }

    @mcp.tool()
    def hq_strategy_configure_account(
        strategy: str,
        account_id: str | None = None,
        client_id: int | None = None,
        ibkr_host: str | None = None,
        ibkr_port: int | None = None,
        contracts: int | None = None,
        dry_run: bool | None = None,
        hold_overnight: bool | None = None,
    ) -> dict[str, Any]:
        """Bind an HQ strategy (nq_v221, es_v221, r2_mnq) to an Agentic IBKR paper trading
        account (must start with 'D', e.g. DUR842608 or DUR842609) and configure contract
        count or dry_run mode. Strategy signal/stop thresholds cannot be changed."""
        authorize()
        return runner.configure_account(
            strategy,
            account_id=account_id,
            client_id=client_id,
            ibkr_host=ibkr_host,
            ibkr_port=ibkr_port,
            contracts=contracts,
            dry_run=dry_run,
            hold_overnight=hold_overnight,
            actor="mcp",
        )

    @mcp.tool()
    def hq_strategy_start(
        strategy: str,
        account_id: str | None = None,
        contracts: int | None = None,
        dry_run: bool | None = None,
        hold_overnight: bool | None = None,
        spawn: bool = True,
    ) -> dict[str, Any]:
        """Start an HQ futures strategy daemon (nq_v221, es_v221, r2_mnq) connected to
        the bound IBKR paper account with locked thresholds. Clears KILL_SWITCH before starting."""
        authorize()
        return runner.start_strategy(
            strategy,
            account_id=account_id,
            contracts=contracts,
            dry_run=dry_run,
            hold_overnight=hold_overnight,
            spawn=spawn,
            actor="mcp",
        )

    @mcp.tool()
    def hq_strategy_halt(
        strategy: str,
        reason: str = "halted via MCP",
        stop_process: bool = False,
    ) -> dict[str, Any]:
        """Engage the strategy's KILL_SWITCH file. On the next cycle, the live daemon
        flattens any open futures position on IBKR, cancels resting stops, and blocks entries."""
        authorize()
        return runner.halt_strategy(strategy, reason=reason, stop_process=stop_process)

    @mcp.tool()
    def hq_strategy_resume(strategy: str) -> dict[str, Any]:
        """Remove the strategy's KILL_SWITCH file so it can resume taking entries."""
        authorize()
        return runner.resume_strategy(strategy)

    @mcp.tool()
    def hq_strategy_evaluate_bars(
        strategy: str,
        bars: list[dict[str, Any]],
        fng_prior_day: float | None = None,
        use_saved_state: bool = False,
    ) -> dict[str, Any]:
        """Evaluate closed 1-min bars through the exact validated V221Engine or R2Engine
        from hq-strategy with locked thresholds. Each bar dict requires open, high, low, close,
        optional volume, vol_price (VXN), and datetime_et."""
        authorize()
        return runner.evaluate_bars(
            strategy,
            bars,
            fng_prior_day=fng_prior_day,
            use_saved_state=use_saved_state,
        )

    @mcp.tool()
    def hq_strategy_selftest(strategy: str) -> dict[str, Any]:
        """Run the strategy's offline --selftest in hq-strategy and verify locked thresholds."""
        authorize()
        return runner.run_selftest(strategy)

    @mcp.tool()
    def ibkr_futures_account(strategy: str = "nq_v221") -> dict[str, Any]:
        """Connect to IBKR Gateway for the strategy's bound paper account and return live
        account summary (NLV, excess liquidity, buying power), front futures contract,
        open positions, and resting V221 GTC stop orders."""
        authorize()
        return runner.ibkr_account_state(strategy)

    @mcp.tool()
    def ibkr_futures_execute(
        strategy: str,
        action: str,
        qty: int | None = None,
        dry_run: bool | None = None,
        reason: str = "grok_bot_mcp",
    ) -> dict[str, Any]:
        """Execute a futures bracket order (BUY or SELL) or FLATTEN on the strategy's bound
        IBKR paper account. Every BUY/SELL automatically attaches a server-side GTC stop at
        the strategy's locked broker_stop_pct (1.0% NQ, 0.7% ES, 0.8% R2 MNQ) and flattens
        immediately if IBKR does not confirm the stop."""
        authorize()
        return runner.ibkr_execute_signal(
            strategy,
            action,
            qty=qty,
            dry_run=dry_run,
            reason=reason,
        )

    @mcp.tool()
    def hq_context_brief(
        strategy: str | None = None,
        question: str = "",
        use_llm: bool = True,
    ) -> dict[str, Any]:
        """Context Language Model (CLM) desk briefing for Claude Cowork and GrokBot:
        fuses live Renko/MCO engine state, sentiment gates, position distance-to-stop X-ray,
        execution slippage/loss-cap drift, and downward-only sleeve allocation (nq_v221,
        es_v221, r2_mnq) with locked thresholds."""
        authorize()
        return clm.generate_brief(strategy, question=question, use_llm=use_llm)

    @mcp.tool()
    def hq_context_position_xray(strategy: str | None = None) -> dict[str, Any]:
        """Real-time Position Risk X-Ray for one or all HQ strategies: exact index price and
        dollar P&L distance to every locked exit level (2/3/4% profit-take, going-nowhere stop,
        after-hours stop, engine hard stop 'fts', and IBKR server-side GTC disaster stop)."""
        authorize()
        return clm.position_xray(strategy)

    @mcp.tool()
    def hq_context_apply_guard(strategy: str | None = None) -> dict[str, Any]:
        """Apply the Context Language Model's downward-only protective verdict on bound IBKR
        paper accounts (STAND_DOWN -> engages KILL_SWITCH; REDUCE -> shrinks contract count).
        Never increases size above validated defaults and never alters thresholds."""
        authorize()
        return clm.apply_recommendation(strategy, actor="claude_cowork_mcp")

    @mcp.tool()
    def hq_context_notify_grokbot(
        strategy: str | None = None,
        question: str = "",
    ) -> dict[str, Any]:
        """Generate a Context Language Model brief and push it to the GrokBot webhook routine."""
        authorize()
        return clm.notify_grokbot(strategy, question=question)

    @mcp.tool()
    def hq_sandbox_datasets() -> dict[str, Any]:
        """Backtest Sandbox discovery: list available historical CSV datasets in the GCS
        bucket (gs://$IBKR_REPORTS_BUCKET/NSDQ250/...) and local mirror, plus the bound
        IBKR paper account for each strategy (nq_v221, es_v221, r2_mnq)."""
        authorize()
        return sbx.list_datasets()

    @mcp.tool()
    def hq_sandbox_backtest_gcs(
        strategy: str,
        start: str | None = None,
        end: str | None = None,
        gcs_key: str | None = None,
        contracts: int | None = None,
        fng_prior_day: float | None = None,
    ) -> dict[str, Any]:
        """Run an isolated Sandbox Backtest of an HQ strategy (nq_v221, es_v221, r2_mnq)
        pointing to 1) historical data in the GCS bucket (gs://$IBKR_REPORTS_BUCKET/NSDQ250/...).
        Uses the strategy's bound paper account metadata and locked thresholds."""
        authorize()
        return sbx.run_gcs_backtest(
            strategy,
            start=start,
            end=end,
            gcs_key=gcs_key,
            contracts=contracts,
            fng_prior_day=fng_prior_day,
        )

    @mcp.tool()
    def hq_sandbox_backtest_ibkr(
        strategy: str,
        days: int = 30,
        bar_size: str = "1 min",
        use_rth: bool = False,
        account_id: str | None = None,
        contracts: int | None = None,
        fng_prior_day: float | None = None,
    ) -> dict[str, Any]:
        """Run an isolated Sandbox Backtest of an HQ strategy (nq_v221, es_v221, r2_mnq)
        pointing to 2) IBKR daily/historical data fetched through the SAME bound IBKR paper
        account (D...). Replays the locked V221/R2 engine and stores the sandbox run."""
        authorize()
        return sbx.run_ibkr_backtest(
            strategy,
            days=days,
            bar_size=bar_size,
            use_rth=use_rth,
            account_id=account_id,
            contracts=contracts,
            fng_prior_day=fng_prior_day,
        )

    @mcp.tool()
    def hq_sandbox_runs(limit: int = 20, run_id: str | None = None) -> dict[str, Any]:
        """List recent Sandbox Backtest runs (both GCS historical and IBKR paper-account
        backtests), or fetch full metrics and trade list for a specific run_id."""
        authorize()
        if run_id:
            found = sbx.get_run(run_id)
            if found is None:
                raise ValueError(f"sandbox run {run_id!r} not found")
            return found
        return {"runs": sbx.list_runs(limit)}


def register_hq_tools(
    mcp: FastMCP,
    reader: HqReader,
    authorize: Callable[[], None],
    runner: HqStrategyRunner | None = None,
    context_lm: HqContextLanguageModel | None = None,
    sandbox: HqBacktestSandbox | None = None,
) -> None:
    """Add ``hq_*`` tools. ``authorize`` raises when the caller's token is not valid."""

    def known(strategy: str) -> str:
        codes = {s["strategy_code"] for s in reader.strategies()}
        if strategy not in codes:
            raise ValueError(f"unknown strategy {strategy!r}; one of {', '.join(sorted(codes))}")
        return strategy

    @mcp.tool()
    def hq_strategies() -> list[dict[str, Any]]:
        """Futures strategies (es_v221, nq_v221, r2_mnq, ...) with the latest ITD scorecard
        (gate, net P&L, return, equity, red/amber/green KPI counts) and latest day status."""
        authorize()
        return reader.strategies()

    @mcp.tool()
    def hq_kpis(strategy: str, as_of: str | None = None) -> dict[str, Any]:
        """Workbook KPI scorecard for one strategy on an as-of date (YYYY-MM-DD, default
        latest): WEEK/MTD/ITD gates plus every KPI value, status and green/amber band."""
        authorize()
        return reader.kpis(known(strategy), _day(as_of, "as_of"))

    @mcp.tool()
    def hq_sync(date: str | None = None) -> dict[str, Any]:
        """Live paper vs backtest replay for every strategy on one session (default latest):
        trades, net P&L, delta $ and %, exit-reason match, loss cap, OK/FLAG/N/A status."""
        authorize()
        dates = reader.sync_dates()
        day = _day(date, "date") or (_day(dates[-1], "date") if dates else None)
        rows = reader.daily_sync(None, day, day) if day else []
        return {"session_date": day.isoformat() if day else None, "dates": dates, "rows": rows}

    @mcp.tool()
    def hq_paper_day(strategy: str | None = None, date: str | None = None) -> dict[str, Any]:
        """Paper engine activity for one session (default latest), one or all strategies:
        trades (open and closed), IB fills, signals entered/blocked, engine events, and data
        freshness (last VM sync, last log line, when the loader read it)."""
        authorize()
        code = known(strategy) if strategy else None
        dates = reader.paper_dates(code)
        day = _day(date, "date") or (_day(dates[-1], "date") if dates else None)
        if day is None:
            return {"strategy_code": code, "session_date": None, "dates": dates}
        return {"dates": dates, **reader.paper_day(code, day)}

    @mcp.tool()
    def hq_compare(strategy: str, date: str | None = None) -> dict[str, Any]:
        """One strategy's session in detail (default latest): daily sync row, paper trades
        with fills/slippage/MAE/MFE, backtest trades and the trade-by-trade matching."""
        authorize()
        code = known(strategy)
        day = _day(date, "date")
        if day is None:
            rows = reader.daily_sync(code)
            if not rows:
                return {"strategy_code": code, "session_date": None}
            day = _day(rows[0]["session_date"], "date")
        assert day is not None
        return reader.comparison(code, day)

    @mcp.tool()
    def hq_daily_pnl(
        strategy: str, start: str | None = None, end: str | None = None
    ) -> list[dict[str, Any]]:
        """Daily P&L for one strategy: trades, gross, commission, slippage, net, equity,
        daily return and ITD drawdown, oldest first. Dates are YYYY-MM-DD."""
        authorize()
        return reader.daily_pnl(known(strategy), _day(start, "start"), _day(end, "end"))

    @mcp.tool()
    def hq_trades(
        strategy: str, start: str | None = None, end: str | None = None
    ) -> list[dict[str, Any]]:
        """Paper trades for one strategy between two sessions (YYYY-MM-DD, inclusive)."""
        authorize()
        return reader.paper_trades(known(strategy), _day(start, "start"), _day(end, "end"))

    @mcp.tool()
    def hq_returns(strategy: str) -> dict[str, Any]:
        """Weekly and monthly returns plus all-time live-vs-backtest sync statistics."""
        authorize()
        return reader.returns(known(strategy))

    @mcp.tool()
    def hq_sentiment(start: str | None = None, end: str | None = None) -> dict[str, Any]:
        """Futures sentiment per session (YYYY-MM-DD range, newest first): Fear & Greed (CNN,
        in-house replica, prior day), VIX and term structure, headline narrative score and
        dispersion, kill-switch hits, scheduled CPI/NFP/PCE/FOMC, the regime, the strategy
        verdicts (TRADE / REDUCE / STAND_DOWN with size) and a plain-language summary."""
        authorize()
        return {
            "dates": reader.sentiment_dates(),
            "days": reader.sentiment_range(_day(start, "start"), _day(end, "end")),
        }

    @mcp.tool()
    def hq_sentiment_now() -> dict[str, Any]:
        """The current sentiment gate: the newest interval's summary, which strategy to run
        (rank, TRADE / REDUCE / STAND_DOWN, size multiplier, regime fit) and every gate's
        state with its reason, confidence and expiry. Ask this first for 'what should we
        trade right now'."""
        authorize()
        return reader.sentiment_latest()

    @mcp.tool()
    def hq_sentiment_gates(
        start: str | None = None,
        end: str | None = None,
        strategy: str | None = None,
        gate: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        """Gate audit trail (newest first, max 500): one row per gate per strategy per
        interval with state, reason, confidence, evidence, input as-of time, expiry, operator
        override, policy version and input hash. gate: data | event | kill | engine | vol |
        positioning; state: OPEN | CAUTION | HALT | BLOCKED | UNKNOWN."""
        authorize()
        code = known(strategy) if strategy else None
        return reader.sentiment_gates(_day(start, "start"), _day(end, "end"), code, gate, state)

    @mcp.tool()
    def hq_sentiment_day(date: str | None = None, strategy: str | None = None) -> dict[str, Any]:
        """One session in full (default latest): every intraday interval summary, the
        per-strategy recommendation and rank, each gate's audit row (state, confidence,
        evidence, expiry, override), every stored score and the scored headlines."""
        authorize()
        code = known(strategy) if strategy else None
        dates = reader.sentiment_dates()
        day = _day(date, "date") or (_day(dates[-1], "date") if dates else None)
        if day is None:
            return {"session_date": None, "dates": dates}
        return {"dates": dates, **reader.sentiment_day(day, code)}

    @mcp.tool()
    def hq_sentiment_series(
        layer: str, component: str, start: str | None = None, end: str | None = None
    ) -> list[dict[str, Any]]:
        """Daily history of one stored score, e.g. layer 'fng' component 'replica',
        'flow'/'vix_term_ratio', 'narrative'/'all', 'positioning'/'cot_lev:NQ',
        'regime'/'chop:NQ'."""
        authorize()
        return reader.sentiment_series(layer, component, _day(start, "start"), _day(end, "end"))

    @mcp.tool()
    def hq_sentiment_quality() -> dict[str, Any]:
        """Sentiment gate quality: per-gate efficacy vs strategy P&L (fired vs open sessions),
        PSI drift alarms, data source health and active operator overrides."""
        authorize()
        return {"efficacy": reader.sentiment_efficacy(), **reader.sentiment_health()}

    @mcp.tool()
    def hq_load_status() -> dict[str, Any]:
        """Last log-loader run per job (paper/backtest/backfill) and recent day statuses
        (paper FINAL/PRELIMINARY/INTRADAY, backtest LOADED/MISSING/PENDING)."""
        authorize()
        return {"loads": reader.load_health(), "days": reader.day_status()[:30]}

    if runner is not None:
        register_hq_strategy_exec_tools(
            mcp,
            runner,
            authorize,
            reader=reader,
            context_lm=context_lm,
            sandbox=sandbox,
        )

