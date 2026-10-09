"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { Check, X } from "lucide-react";

import { Chip, num, signTone, stamp, time, usd } from "@/components/hq";
import { Section } from "@/components/hq-views";
import QueryGate from "@/components/query-gate";
import { getHqCompare } from "@/lib/api";
import { capWasHit, summarize, type DaySummary, type TradePair } from "@/lib/hq-summary";
import type { HqCompare, HqStrategy } from "@/lib/types";

const longDate = (d: string) =>
  new Date(`${d}T12:00:00Z`).toLocaleDateString("en-US", {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });

const REFRESH_MS = 15 * 60 * 1000;

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

function LoadTimes({ d }: { d: HqCompare }) {
  const st = d.context.day_status;
  if (!st) return null;
  const bits = [
    `Paper log loaded ${stamp(st.paper_loaded_at)}`,
    st.finalized_at ? `day finalized ${stamp(st.finalized_at)}` : "day not final yet",
    st.backtest_loaded_at
      ? `backtest replay loaded ${stamp(st.backtest_loaded_at)}`
      : "backtest replay not loaded yet (runs 16:35 and 17:35 ET)",
  ];
  return <div className="mx-5 -mt-2 mb-3 text-xs text-slate-500">{bits.join(" · ")}</div>;
}

function Same({ ok }: { ok: boolean | null }) {
  if (ok == null) return <span className="text-slate-600">—</span>;
  return ok ? <Check size={14} className="text-emerald-400" /> : <X size={14} className="text-rose-400" />;
}

type Side = {
  trades: number;
  wins: number;
  losses: number;
  directions: string;
  contracts: string;
  firstEntry: string;
  lastExit: string;
  exitReasons: string;
  points: number | null;
  gross: number | null;
  commission: number | null;
  net: number | null;
};

const total = (xs: (number | null | undefined)[]) =>
  xs.length && xs.every((x) => x != null) ? xs.reduce<number>((a, x) => a + (x as number), 0) : null;

function tally(values: (string | null | undefined)[]): string {
  const counts = new Map<string, number>();
  for (const v of values) counts.set(v ?? "—", (counts.get(v ?? "—") ?? 0) + 1);
  if (counts.size === 0) return "—";
  return [...counts].map(([k, n]) => (n > 1 ? `${k} ×${n}` : k)).join(", ");
}

const at = (ts: string | null | undefined, px: number | null | undefined) =>
  ts ? `${time(ts)}${px != null ? ` @ ${num(px)}` : ""}` : "—";

function liveSide(d: HqCompare): Side {
  const t = d.paper_trades;
  const nets = t.map((x) => x.net_pnl);
  return {
    trades: t.length,
    wins: nets.filter((n) => n != null && n > 0).length,
    losses: nets.filter((n) => n != null && n < 0).length,
    directions: tally(t.map((x) => x.direction)),
    contracts: tally(t.map((x) => String(x.contracts))),
    firstEntry: t.length ? at(t[0].entry_ts, t[0].entry_px) : "—",
    lastExit: t.length ? at(t[t.length - 1].exit_ts, t[t.length - 1].exit_px) : "—",
    exitReasons: tally(t.map((x) => x.exit_reason)),
    points: total(t.map((x) => x.points)),
    gross: total(t.map((x) => x.gross_pnl)),
    commission: total(t.map((x) => x.commission)),
    net: d.sync?.live_net_pnl ?? total(nets),
  };
}

function backtestSide(d: HqCompare): Side {
  const t = d.backtest_trades;
  const pv = d.context.backtest_run?.point_value ?? d.context.settings?.point_value ?? null;
  const gross = t.map((x) => (x.points != null && x.contracts != null && pv != null ? x.points * pv * x.contracts : null));
  const net = d.sync?.bt_net_pnl ?? total(t.map((x) => x.net_pnl));
  const grossTotal = total(gross);
  const nets = t.map((x) => x.net_pnl);
  return {
    trades: t.length,
    wins: nets.filter((n) => n != null && n > 0).length,
    losses: nets.filter((n) => n != null && n < 0).length,
    directions: tally(t.map((x) => x.direction)),
    contracts: tally(t.map((x) => `${x.contracts ?? "—"}${x.contracts_inferred ? " (default)" : ""}`)),
    firstEntry: t.length ? at(t[0].entry_ts, t[0].entry_px) : "—",
    lastExit: t.length ? at(t[t.length - 1].exit_ts, t[t.length - 1].exit_px) : "—",
    exitReasons: tally(t.map((x) => x.exit_reason)),
    points: total(t.map((x) => x.points)),
    gross: grossTotal,
    commission: grossTotal != null && net != null ? grossTotal - net : null,
    net,
  };
}

const money = (n: number | null) => <span className={signTone(n)}>{usd(n)}</span>;
const diff = (a: number | null, b: number | null) => (a != null && b != null ? a - b : null);

function DayAtAGlance({ d }: { d: HqCompare }) {
  const l = liveSide(d);
  const hasBt = d.context.backtest_run != null;
  const b = hasBt ? backtestSide(d) : null;
  const same = (x: string | number, y: string | number | undefined) => (b ? x === y : null);
  const minute = (s: string) => s.slice(0, 5);
  const capHit = capWasHit(d.sync?.loss_cap_hit);
  type Row = [string, React.ReactNode, React.ReactNode, React.ReactNode];
  const rows: Row[] = [
    ["Trades", l.trades, b ? b.trades : "—", <Same key="t" ok={same(l.trades, b?.trades)} />],
    ["Wins / losses", `${l.wins} / ${l.losses}`, b ? `${b.wins} / ${b.losses}` : "—", <Same key="w" ok={same(`${l.wins}/${l.losses}`, b ? `${b.wins}/${b.losses}` : undefined)} />],
    ["Direction", l.directions, b?.directions ?? "—", <Same key="d" ok={same(l.directions, b?.directions)} />],
    ["Contracts per trade", l.contracts, b?.contracts ?? "—", <Same key="c" ok={b ? l.contracts === b.contracts.replace(" (default)", "") : null} />],
    ["First entry (ET)", l.firstEntry, b?.firstEntry ?? "—", <Same key="e" ok={b && l.trades && b.trades ? minute(l.firstEntry) === minute(b.firstEntry) : null} />],
    ["Last exit (ET)", l.lastExit, b?.lastExit ?? "—", <Same key="x" ok={b && l.trades && b.trades ? minute(l.lastExit) === minute(b.lastExit) : null} />],
    ["Exit reasons", l.exitReasons, b?.exitReasons ?? "—", <Same key="r" ok={same(l.exitReasons, b?.exitReasons)} />],
    ["Points", <span key="lp" className={signTone(l.points)}>{num(l.points)}</span>, <span key="bp" className={signTone(b?.points)}>{num(b?.points)}</span>, <span key="gp" className={signTone(diff(l.points, b?.points ?? null))}>{num(diff(l.points, b?.points ?? null))}</span>],
    ["Gross P&L", money(l.gross), money(b?.gross ?? null), money(diff(l.gross, b?.gross ?? null))],
    ["Commission / costs", usd(l.commission), usd(b?.commission ?? null), usd(diff(l.commission, b?.commission ?? null))],
    ["Net P&L", money(l.net), money(b?.net ?? null), money(diff(l.net, b?.net ?? null))],
    ["Daily loss cap", capHit ? "Triggered" : "Not triggered", "—", ""],
  ];
  return (
    <table className="hq-table w-full text-sm">
      <thead className="text-left text-slate-500">
        <tr>
          <th className="px-5 py-2">Day at a glance</th>
          <th>Live paper (actual)</th>
          <th>Backtest replay</th>
          <th className="pr-5">Same? / live − backtest</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([label, live, bt, cmp]) => (
          <tr key={label} className="border-t border-slate-800">
            <td className="px-5 py-1.5 text-slate-400">{label}</td>
            <td className="wrap-cell">{live}</td>
            <td className="wrap-cell">{bt}</td>
            <td className="pr-5">{cmp}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function TradeByTrade({ pairs, d }: { pairs: TradePair[]; d: HqCompare }) {
  const s = d.sync;
  return (
    <div className="border-t border-slate-800">
      <div className="px-5 pt-3 text-xs uppercase tracking-wide text-slate-500">Trade by trade</div>
      <table className="hq-table w-full text-sm">
        <thead className="text-left text-slate-500">
          <tr>
            <th className="px-5 py-2">#</th>
            <th>Side</th>
            <th>Live entry → exit (ET)</th>
            <th>Backtest entry → exit (ET)</th>
            <th>Exit reason (live / backtest)</th>
            <th>Points (live / backtest)</th>
            <th>Point gap</th>
            <th>Net (live / backtest)</th>
            <th className="pr-5">Status</th>
          </tr>
        </thead>
        <tbody>
          {pairs.length === 0 ? (
            <tr className="border-t border-slate-800">
              <td className="px-5 py-1.5 text-slate-500" colSpan={9}>
                No trades on either side this session.
              </td>
            </tr>
          ) : null}
          {pairs.map((p) => {
            const gap = p.live?.points != null && p.bt?.points != null ? p.live.points - p.bt.points : null;
            return (
              <tr key={p.n} className="border-t border-slate-800">
                <td className="px-5 py-1.5">{p.n}</td>
                <td>
                  {p.live && p.bt && p.live.direction !== p.bt.direction
                    ? `${p.live.direction} / ${p.bt.direction}`
                    : (p.live?.direction ?? p.bt?.direction)}
                </td>
                <td>{p.live ? `${at(p.live.entry_ts, p.live.entry_px)} → ${at(p.live.exit_ts, p.live.exit_px)}` : "—"}</td>
                <td>{p.bt ? `${at(p.bt.entry_ts, p.bt.entry_px)} → ${at(p.bt.exit_ts, p.bt.exit_px)}` : "—"}</td>
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
          {pairs.length ? (
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
          ) : null}
        </tbody>
      </table>
    </div>
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
        <LoadTimes d={d} />
        <DayAtAGlance d={d} />
        <TradeByTrade pairs={summary.pairs} d={d} />
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
    refetchInterval: REFRESH_MS,
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
      refetchInterval: REFRESH_MS,
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
