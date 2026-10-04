"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { Check, X } from "lucide-react";

import { Chip, num, signTone, time, usd } from "@/components/hq";
import { Section } from "@/components/hq-views";
import QueryGate from "@/components/query-gate";
import { getHqCompare } from "@/lib/api";
import { summarize, type DaySummary, type TradePair } from "@/lib/hq-summary";
import type { HqCompare, HqStrategy } from "@/lib/types";

const longDate = (d: string) =>
  new Date(`${d}T12:00:00Z`).toLocaleDateString("en-US", {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });

const TONE = {
  good: "border-emerald-500/60 bg-emerald-500/5",
  bad: "border-rose-500/60 bg-rose-500/5",
  warn: "border-amber-500/60 bg-amber-500/5",
  neutral: "border-slate-600 bg-slate-800/30",
} as const;

function SummaryBox({ summary }: { summary: DaySummary }) {
  return (
    <div className={`mx-5 my-4 rounded-lg border-l-4 px-4 py-3 ${TONE[summary.tone]}`}>
      <div className="text-xs uppercase tracking-wide text-slate-500">Summary</div>
      <p className="mt-1 font-medium text-slate-100">{summary.headline}</p>
      {summary.sentences.length ? (
        <p className="mt-2 text-sm leading-relaxed text-slate-300">{summary.sentences.join(" ")}</p>
      ) : null}
      {summary.warnings.length ? (
        <ul className="mt-3 space-y-1 text-sm text-amber-200">
          {summary.warnings.map((w) => (
            <li key={w}>⚠ {w}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function Same({ ok }: { ok: boolean | null }) {
  if (ok == null) return <span className="text-slate-600">—</span>;
  return ok ? <Check size={14} className="text-emerald-400" /> : <X size={14} className="text-rose-400" />;
}

function SideBySide({ pair, d }: { pair: TradePair | undefined; d: HqCompare }) {
  const l = pair?.live ?? null;
  const b = pair?.bt ?? null;
  const s = d.sync;
  const gap = l?.points != null && b?.points != null ? l.points - b.points : null;
  const both = Boolean(l && b);
  const rows: [string, React.ReactNode, React.ReactNode, boolean | null][] = [
    ["Direction", l?.direction ?? "—", b?.direction ?? "—", both ? l!.direction === b!.direction : null],
    ["Contracts", l?.contracts ?? "—", b ? `${b.contracts ?? "—"}${b.contracts_inferred ? " (default)" : ""}` : "—", both ? l!.contracts === b!.contracts : null],
    ["Entry (ET)", l ? `${time(l.entry_ts)} @ ${num(l.entry_px)}` : "—", b ? `${time(b.entry_ts)} @ ${num(b.entry_px)}` : "—", both ? time(l!.entry_ts) === time(b!.entry_ts) : null],
    ["Exit (ET)", l ? `${time(l.exit_ts)} @ ${num(l.exit_px)}` : "—", b ? `${time(b.exit_ts)} @ ${num(b.exit_px)}` : "—", both ? time(l!.exit_ts) === time(b!.exit_ts) : null],
    ["Exit reason", l?.exit_reason ?? "—", b?.exit_reason ?? "—", both ? l!.exit_reason === b!.exit_reason : null],
    [
      "Points",
      <span key="l" className={signTone(l?.points)}>{num(l?.points)}</span>,
      <span key="b" className={signTone(b?.points)}>{num(b?.points)}</span>,
      null,
    ],
    ["Point gap (live − backtest)", gap == null ? "—" : <span className={signTone(gap)}>{num(gap)}</span>, "", null],
    [
      "Net P&L",
      <span key="l" className={signTone(s?.live_net_pnl)}>{usd(s?.live_net_pnl)}</span>,
      <span key="b" className={signTone(s?.bt_net_pnl)}>{usd(s?.bt_net_pnl)}</span>,
      null,
    ],
    ["Daily loss cap", s?.loss_cap_hit ? "Triggered" : "Not triggered", "", null],
  ];
  return (
    <table className="hq-table w-full text-sm">
      <thead className="text-left text-slate-500">
        <tr>
          <th className="px-5 py-2">Metric</th>
          <th>Live paper (actual)</th>
          <th>Backtest replay</th>
          <th className="pr-5">Same?</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([label, live, bt, same]) => (
          <tr key={label} className="border-t border-slate-800">
            <td className="px-5 py-1.5 text-slate-400">{label}</td>
            <td>{live}</td>
            <td>{bt}</td>
            <td className="pr-5">
              <Same ok={same} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function TradeByTrade({ pairs, d }: { pairs: TradePair[]; d: HqCompare }) {
  const s = d.sync;
  return (
    <table className="hq-table w-full text-sm">
      <thead className="text-left text-slate-500">
        <tr>
          <th className="px-5 py-2">#</th>
          <th>Side</th>
          <th>Live entry → exit</th>
          <th>Backtest entry → exit</th>
          <th>Exit reason (live / backtest)</th>
          <th>Points (live / backtest)</th>
          <th>Point gap</th>
          <th>Net (live / backtest)</th>
          <th className="pr-5">Status</th>
        </tr>
      </thead>
      <tbody>
        {pairs.map((p) => {
          const gap = p.live?.points != null && p.bt?.points != null ? p.live.points - p.bt.points : null;
          return (
            <tr key={p.n} className="border-t border-slate-800">
              <td className="px-5 py-1.5">{p.n}</td>
              <td>{p.live?.direction ?? p.bt?.direction}</td>
              <td>{p.live ? `${time(p.live.entry_ts)} → ${time(p.live.exit_ts)}` : "—"}</td>
              <td>{p.bt ? `${time(p.bt.entry_ts)} → ${time(p.bt.exit_ts)}` : "—"}</td>
              <td>
                {p.live?.exit_reason ?? "—"} / {p.bt?.exit_reason ?? "—"}
              </td>
              <td>
                {num(p.live?.points)} / {num(p.bt?.points)}
              </td>
              <td className={signTone(gap)}>{num(gap)}</td>
              <td>
                <span className={signTone(p.live?.net_pnl)}>{usd(p.live?.net_pnl)}</span> /{" "}
                <span className={signTone(p.bt?.net_pnl)}>{usd(p.bt?.net_pnl)}</span>
              </td>
              <td className="wrap-cell pr-5">
                <Chip value={p.match?.unmatched_reason ?? p.match?.match_type ?? (p.live ? "LIVE" : "BACKTEST")} />
              </td>
            </tr>
          );
        })}
        <tr className="border-t border-slate-700 font-medium">
          <td className="px-5 py-1.5" colSpan={5}>
            Day total
          </td>
          <td>
            {num(s?.live_points)} / {num(s?.bt_points)}
          </td>
          <td />
          <td>
            <span className={signTone(s?.live_net_pnl)}>{usd(s?.live_net_pnl)}</span> /{" "}
            <span className={signTone(s?.bt_net_pnl)}>{usd(s?.bt_net_pnl)}</span>
          </td>
          <td className="pr-5">
            <Chip value={s?.sync_status} />
          </td>
        </tr>
      </tbody>
    </table>
  );
}

function DollarMath({ summary }: { summary: DaySummary }) {
  if (summary.checks.length === 0) return null;
  return (
    <div className="border-t border-slate-800">
      <div className="px-5 pt-3 text-xs uppercase tracking-wide text-slate-500">Independent dollar math check</div>
      <table className="hq-table w-full text-sm">
        <thead className="text-left text-slate-500">
          <tr>
            <th className="px-5 py-2">Side</th>
            <th>Check</th>
            <th>Formula</th>
            <th>Computed</th>
            <th>Recorded</th>
            <th className="pr-5">Result</th>
          </tr>
        </thead>
        <tbody>
          {summary.checks.map((c) => (
            <tr key={`${c.side}-${c.label}`} className="border-t border-slate-800">
              <td className="px-5 py-1.5">{c.side}</td>
              <td>{c.label}</td>
              <td className="text-slate-400">{c.formula}</td>
              <td>{usd(c.expected)}</td>
              <td>{usd(c.actual)}</td>
              <td className="pr-5">
                <Same ok={c.ok} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ExecutionDetails({ d }: { d: HqCompare }) {
  if (d.paper_trades.length === 0) return null;
  return (
    <Section title="Live execution details" subtitle="Fills, slippage and excursions for every paper trade">
      <table className="hq-table w-full text-sm">
        <thead className="text-left text-slate-500">
          <tr>
            <th className="px-5 py-2">#</th>
            <th>Side</th>
            <th>Qty</th>
            <th>Entry (ET)</th>
            <th>Exit (ET)</th>
            <th>Exit reason</th>
            <th>Points</th>
            <th>Slippage pts (in / out)</th>
            <th>Gross</th>
            <th>Commission</th>
            <th>Net</th>
            <th>Held</th>
            <th className="pr-5">MAE / MFE pts</th>
          </tr>
        </thead>
        <tbody>
          {d.paper_trades.map((t) => (
            <tr key={`${t.trade_no}-${t.entry_ts}`} className="border-t border-slate-800">
              <td className="px-5 py-1.5">{t.trade_no}</td>
              <td>{t.direction}</td>
              <td>{t.contracts}</td>
              <td>
                {time(t.entry_ts)} @ {num(t.entry_px)}
              </td>
              <td>
                {time(t.exit_ts)} @ {num(t.exit_px)}
              </td>
              <td>{t.exit_reason ?? "—"}</td>
              <td className={signTone(t.points)}>{num(t.points)}</td>
              <td>
                {num(t.entry_slip_pts)} / {num(t.exit_slip_pts)}
              </td>
              <td>{usd(t.gross_pnl)}</td>
              <td>{usd(t.commission)}</td>
              <td className={signTone(t.net_pnl)}>{usd(t.net_pnl)}</td>
              <td>{t.hold_min != null ? `${Math.round(t.hold_min)}m` : "—"}</td>
              <td className="pr-5">
                {num(t.mae_pts)} / {num(t.mfe_pts)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function StrategyReport({ strategy, d, details }: { strategy: HqStrategy; d: HqCompare; details: boolean }) {
  const summary = summarize(strategy.display_name, d);
  const label = d.context.backtest_run?.config_label ?? d.context.live_params?.config_label;
  return (
    <>
      <Section
        title={`${strategy.display_name} (${strategy.instrument_root}) — Live vs. Backtest`}
        subtitle={`Trade date: ${longDate(d.session_date)}${label ? ` · Config: ${label}` : ""}`}
        right={
          d.sync ? (
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
              paper <Chip value={d.sync.paper_status} /> backtest <Chip value={d.sync.backtest_status} /> result{" "}
              <Chip value={d.sync.sync_status} />
            </div>
          ) : null
        }
      >
        <SummaryBox summary={summary} />
        {summary.pairs.length === 0 ? (
          <div className="px-5 pb-4 text-sm text-slate-500">No trades on either side this session.</div>
        ) : summary.pairs.length === 1 ? (
          <SideBySide pair={summary.pairs[0]} d={d} />
        ) : (
          <TradeByTrade pairs={summary.pairs} d={d} />
        )}
        <DollarMath summary={summary} />
      </Section>
      {details ? <ExecutionDetails d={d} /> : null}
    </>
  );
}

export function StrategyDay({ strategy, date }: { strategy: HqStrategy; date: string }) {
  const cmp = useQuery({
    queryKey: ["hq-compare", strategy.strategy_code, date],
    queryFn: () => getHqCompare(strategy.strategy_code, date),
  });
  if (cmp.isLoading || cmp.isError) {
    return <QueryGate isLoading={cmp.isLoading} isError={cmp.isError} error={cmp.error} />;
  }
  return <StrategyReport strategy={strategy} d={cmp.data!} details />;
}

export function AllStrategiesReport({
  date,
  strategies,
  onPick,
}: {
  date: string;
  strategies: HqStrategy[];
  onPick: (code: string) => void;
}) {
  const results = useQueries({
    queries: strategies.map((s) => ({
      queryKey: ["hq-compare", s.strategy_code, date],
      queryFn: () => getHqCompare(s.strategy_code, date),
    })),
  });
  const loaded = strategies
    .map((s, i) => ({ s, d: results[i].data }))
    .filter((r): r is { s: HqStrategy; d: HqCompare } => Boolean(r.d));
  if (results.some((r) => r.isLoading)) return <QueryGate isLoading isError={false} />;
  const traded = loaded.filter((r) => r.d.sync?.live_net_pnl != null);
  const liveTotal = traded.reduce((a, r) => a + (r.d.sync!.live_net_pnl ?? 0), 0);
  const btRows = traded.filter((r) => r.d.sync!.bt_net_pnl != null);
  const btTotal = btRows.reduce((a, r) => a + (r.d.sync!.bt_net_pnl ?? 0), 0);
  return (
    <>
      <Section title={`All strategies — ${longDate(date)}`} subtitle="One line per strategy · open a strategy for the full report">
        <div className="space-y-2 px-5 py-4 text-sm">
          {loaded.map(({ s, d }) => (
            <button
              key={s.strategy_code}
              onClick={() => onPick(s.strategy_code)}
              className="block w-full rounded-lg px-3 py-2 text-left hover:bg-slate-800/50"
            >
              <span className="text-slate-200">{summarize(s.display_name, d).headline}</span>
            </button>
          ))}
          {traded.length ? (
            <div className="border-t border-slate-800 px-3 pt-3 text-slate-300">
              Combined live result: <span className={signTone(liveTotal)}>{usd(liveTotal)}</span>
              {btRows.length ? (
                <>
                  {" "}
                  vs backtest <span className={signTone(btTotal)}>{usd(btTotal)}</span>
                  {btRows.length < traded.length ? ` (backtest for ${btRows.length} of ${traded.length})` : ""}
                </>
              ) : null}
              .
            </div>
          ) : null}
        </div>
      </Section>
      {loaded.map(({ s, d }) => (
        <StrategyReport key={s.strategy_code} strategy={s} d={d} details={false} />
      ))}
    </>
  );
}
