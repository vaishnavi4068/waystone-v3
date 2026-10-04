"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { Chip, frac, kpiTarget, kpiValue, num, signTone, time, usd } from "@/components/hq";
import QueryGate from "@/components/query-gate";
import { getHqCompare, getHqDaily, getHqKpis, getHqReturns, getHqStrategy } from "@/lib/api";
import type { HqKpi, HqSyncRow } from "@/lib/types";

const WINDOWS = ["ITD", "MTD", "WEEK"] as const;

function Section({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="card mb-6 overflow-x-auto">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-5 py-3">
        <div className="font-medium">{title}</div>
        {right}
      </div>
      {children}
    </div>
  );
}

function Picker({ value, options, onChange }: { value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <select className="rounded bg-slate-900 px-2 py-1 text-sm" value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((d) => (
        <option key={d} value={d}>
          {d}
        </option>
      ))}
    </select>
  );
}

function KpiPanel({ code, dates }: { code: string; dates: string[] }) {
  const [asOf, setAsOf] = useState<string | undefined>();
  const [win, setWin] = useState<(typeof WINDOWS)[number]>("ITD");
  const kpis = useQuery({ queryKey: ["hq-kpis", code, asOf], queryFn: () => getHqKpis(code, asOf) });
  if (kpis.isLoading || kpis.isError) {
    return <QueryGate isLoading={kpis.isLoading} isError={kpis.isError} error={kpis.error} />;
  }
  const data = kpis.data!;
  if (!data.as_of_date) {
    return <div className="card mb-6 p-5 text-sm text-slate-400">No KPIs yet. They appear after the first paper log loads.</div>;
  }
  const sections = new Map<string, HqKpi[]>();
  for (const k of data.kpis.filter((k) => k.kpi_window === win)) {
    sections.set(k.section_name, [...(sections.get(k.section_name) ?? []), k]);
  }
  return (
    <>
      <div className="mb-4 grid gap-4 md:grid-cols-3">
        {WINDOWS.map((w) => {
          const c = data.scorecard.find((s) => s.kpi_window === w);
          return (
            <button
              key={w}
              onClick={() => setWin(w)}
              className={`card p-4 text-left ${win === w ? "border-emerald-500/60" : "hover:border-slate-600"}`}
            >
              <div className="flex items-center justify-between">
                <div className="text-xs uppercase text-slate-500">
                  {w} · {c?.window_start ?? "—"} → {c?.window_end ?? "—"}
                </div>
              </div>
              <div className="mt-2">
                <Chip wrap value={c?.overall_gate} />
              </div>
              <div className="mt-3 grid grid-cols-3 gap-2 text-sm">
                <div>
                  <div className="text-xs text-slate-500">Net</div>
                  <div className={signTone(c?.net_pnl)}>{usd(c?.net_pnl)}</div>
                </div>
                <div>
                  <div className="text-xs text-slate-500">Return</div>
                  <div className={signTone(c?.return_pct)}>{frac(c?.return_pct)}</div>
                </div>
                <div>
                  <div className="text-xs text-slate-500">Days / trades</div>
                  <div>
                    {c?.trading_days ?? "—"} / {c?.trades ?? "—"}
                  </div>
                </div>
              </div>
            </button>
          );
        })}
      </div>
      <Section
        title={`KPIs · ${win} · as of ${data.as_of_date}`}
        right={dates.length ? <Picker value={data.as_of_date} options={[...dates].reverse()} onChange={setAsOf} /> : null}
      >
        <table className="hq-table w-full text-sm">
          <thead className="bg-slate-900/60 text-left text-slate-500">
            <tr>
              <th className="px-5 py-2">KPI</th>
              <th>Value</th>
              <th>Status</th>
              <th>Green</th>
              <th className="pr-5">Amber</th>
            </tr>
          </thead>
          {[...sections.entries()].map(([name, rows]) => (
            <tbody key={name}>
              <tr className="border-t border-slate-800 bg-slate-900/40">
                <td colSpan={5} className="px-5 py-1.5 text-xs uppercase tracking-wide text-slate-500">
                  {name}
                </td>
              </tr>
              {rows.map((k) => (
                <tr key={k.kpi_code} className="border-t border-slate-800 align-top">
                  <td className="wrap-cell px-5 py-2">
                    <div className="font-medium">{k.label}</div>
                    {k.description ? <div className="mt-1 max-w-lg text-xs text-slate-500">{k.description}</div> : null}
                  </td>
                  <td className="whitespace-nowrap">{kpiValue(k)}</td>
                  <td>{k.section === "header" ? null : <Chip value={k.status} />}</td>
                  <td className="text-slate-400">{kpiTarget(k, k.green_at)}</td>
                  <td className="pr-5 text-slate-400">{kpiTarget(k, k.amber_at)}</td>
                </tr>
              ))}
            </tbody>
          ))}
        </table>
      </Section>
    </>
  );
}

function SyncSummary({ s }: { s: HqSyncRow }) {
  return (
    <div className="grid gap-4 px-5 py-4 text-sm md:grid-cols-6">
      <div>
        <div className="text-xs text-slate-500">Live</div>
        <div className={signTone(s.live_net_pnl)}>{usd(s.live_net_pnl)}</div>
        <div className="text-xs text-slate-500">{s.live_trades ?? 0} trade(s) · {num(s.live_points)} pts</div>
      </div>
      <div>
        <div className="text-xs text-slate-500">Backtest</div>
        <div className={signTone(s.bt_net_pnl)}>{usd(s.bt_net_pnl)}</div>
        <div className="text-xs text-slate-500">{s.bt_trades ?? "—"} trade(s) · {num(s.bt_points)} pts</div>
      </div>
      <div>
        <div className="text-xs text-slate-500">Δ live − backtest</div>
        <div className={signTone(s.pnl_delta)}>{usd(s.pnl_delta)}</div>
        <div className="text-xs text-slate-500">{frac(s.pnl_delta_pct, 1)}</div>
      </div>
      <div>
        <div className="text-xs text-slate-500">Exit reason</div>
        <div>{s.live_exit_reason ?? "—"}</div>
        <div className="text-xs text-slate-500">bt: {s.bt_exit_reason ?? "—"}</div>
      </div>
      <div className="flex flex-col gap-1">
        <div className="text-xs text-slate-500">Status</div>
        <div className="flex flex-wrap gap-1">
          <Chip value={s.sync_status} /> <Chip value={s.paper_status} /> <Chip value={s.backtest_status} />
        </div>
      </div>
      <div className="text-xs text-slate-500">
        {[s.loss_cap_hit ? "Loss cap hit" : null, s.notes_auto, s.notes_manual].filter(Boolean).join(" · ") || "—"}
      </div>
    </div>
  );
}

function DayPanel({ code, dates, initial }: { code: string; dates: string[]; initial?: string }) {
  const [date, setDate] = useState<string | undefined>(initial);
  const cmp = useQuery({ queryKey: ["hq-compare", code, date], queryFn: () => getHqCompare(code, date), retry: false });
  if (cmp.isLoading) return <QueryGate isLoading isError={false} />;
  if (cmp.isError || !cmp.data) {
    return <div className="card mb-6 p-5 text-sm text-slate-400">No sessions loaded yet.</div>;
  }
  const d = cmp.data;
  return (
    <Section
      title={`Session ${d.session_date}`}
      right={dates.length ? <Picker value={d.session_date} options={dates} onChange={setDate} /> : null}
    >
      {d.sync ? <SyncSummary s={d.sync} /> : null}
      <div className="border-t border-slate-800 px-5 pt-3 text-xs uppercase text-slate-500">Paper trades</div>
      <table className="hq-table w-full text-sm">
        <thead className="text-left text-slate-500">
          <tr>
            <th className="px-5 py-2">#</th>
            <th>Side</th>
            <th>Qty</th>
            <th>Entry</th>
            <th>Exit</th>
            <th>Reason</th>
            <th>Points</th>
            <th>Slip (pts in/out)</th>
            <th>Commission</th>
            <th>Net</th>
            <th>Held</th>
            <th className="pr-5">MAE / MFE</th>
          </tr>
        </thead>
        <tbody>
          {d.paper_trades.length === 0 ? (
            <tr className="border-t border-slate-800">
              <td colSpan={12} className="px-5 py-2 text-slate-500">
                No paper trades this session.
              </td>
            </tr>
          ) : (
            d.paper_trades.map((t) => (
              <tr key={t.trade_no} className="border-t border-slate-800">
                <td className="px-5 py-2">{t.trade_no}</td>
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
                <td>{usd(t.commission)}</td>
                <td className={signTone(t.net_pnl)}>{usd(t.net_pnl)}</td>
                <td>{t.hold_min != null ? `${Math.round(t.hold_min)}m` : "—"}</td>
                <td className="pr-5">
                  {num(t.mae_pts)} / {num(t.mfe_pts)}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
      <div className="border-t border-slate-800 px-5 pt-3 text-xs uppercase text-slate-500">Trade matching</div>
      <table className="hq-table mb-2 w-full text-sm">
        <thead className="text-left text-slate-500">
          <tr>
            <th className="px-5 py-2">Match</th>
            <th>Live entry → exit</th>
            <th>Backtest entry → exit</th>
            <th>Exit reason (live / bt)</th>
            <th>Points (live / bt)</th>
            <th>Net (live / bt)</th>
            <th className="pr-5">Δ</th>
          </tr>
        </thead>
        <tbody>
          {d.matches.length === 0 ? (
            <tr className="border-t border-slate-800">
              <td colSpan={7} className="px-5 py-2 text-slate-500">
                Nothing to match (no backtest replay for this session).
              </td>
            </tr>
          ) : (
            d.matches.map((m) => (
              <tr key={m.match_seq} className="border-t border-slate-800">
                <td className="wrap-cell px-5 py-2">
                  <Chip value={m.match_type} />
                  {m.unmatched_reason ? <div className="mt-1 text-xs text-slate-500">{m.unmatched_reason}</div> : null}
                </td>
                <td>
                  {time(m.live_entry_ts)} → {time(m.live_exit_ts)}
                </td>
                <td>
                  {time(m.bt_entry_ts)} → {time(m.bt_exit_ts)}
                </td>
                <td>
                  {m.live_exit_reason ?? "—"} / {m.bt_exit_reason ?? "—"}
                </td>
                <td>
                  {num(m.live_points)} / {num(m.bt_points)}
                </td>
                <td>
                  {usd(m.live_net_pnl)} / {usd(m.bt_net_pnl)}
                </td>
                <td className={`pr-5 ${signTone(m.pnl_delta)}`}>{usd(m.pnl_delta)}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </Section>
  );
}

function History({ code }: { code: string }) {
  const daily = useQuery({ queryKey: ["hq-daily", code], queryFn: () => getHqDaily(code) });
  const returns = useQuery({ queryKey: ["hq-returns", code], queryFn: () => getHqReturns(code) });
  const sync = new Map((daily.data?.sync ?? []).map((s) => [s.session_date, s]));
  const roll = returns.data?.sync_rolling;
  return (
    <>
      {returns.data ? (
        <div className="mb-6 grid gap-4 lg:grid-cols-2">
          <Section title="Weekly returns">
            <table className="hq-table w-full text-sm">
              <thead className="text-left text-slate-500">
                <tr>
                  <th className="px-5 py-2">Week ending</th>
                  <th>Trades</th>
                  <th>Net</th>
                  <th>Return</th>
                  <th className="pr-5">Max DD</th>
                </tr>
              </thead>
              <tbody>
                {[...returns.data.weekly].reverse().map((w) => (
                  <tr key={w.week_start} className="border-t border-slate-800">
                    <td className="px-5 py-1.5">{w.week_end}</td>
                    <td>{w.trades}</td>
                    <td className={signTone(w.net_pnl)}>{usd(w.net_pnl)}</td>
                    <td className={signTone(w.return_pct)}>{frac(w.return_pct)}</td>
                    <td className="pr-5">{frac(w.max_dd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
          <Section title="Monthly returns">
            <table className="hq-table w-full text-sm">
              <thead className="text-left text-slate-500">
                <tr>
                  <th className="px-5 py-2">Month</th>
                  <th>Trades</th>
                  <th>Net</th>
                  <th>Return</th>
                  <th>Max DD</th>
                  <th className="pr-5">Sharpe</th>
                </tr>
              </thead>
              <tbody>
                {[...returns.data.monthly].reverse().map((m) => (
                  <tr key={m.month_start} className="border-t border-slate-800">
                    <td className="px-5 py-1.5">{m.month_start.slice(0, 7)}</td>
                    <td>{m.trades}</td>
                    <td className={signTone(m.net_pnl)}>{usd(m.net_pnl)}</td>
                    <td className={signTone(m.return_pct)}>{frac(m.return_pct)}</td>
                    <td>{frac(m.max_dd)}</td>
                    <td className="pr-5">{num(m.sharpe)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {roll ? (
              <div className="border-t border-slate-800 px-5 py-3 text-xs text-slate-500">
                Sync to date: {roll.days_logged} day(s) · live {usd(roll.all_time_live_pnl)} vs backtest{" "}
                {usd(roll.all_time_bt_pnl)} · {roll.all_time_flags} flag(s) ({frac(roll.flag_rate, 0)}) · last 7
                days live {usd(roll.last7_live_pnl)}
              </div>
            ) : null}
          </Section>
        </div>
      ) : null}
      {daily.data ? (
        <Section title="Daily P&L">
          <table className="hq-table w-full text-sm">
            <thead className="text-left text-slate-500">
              <tr>
                <th className="px-5 py-2">Session</th>
                <th>Trades</th>
                <th>Gross</th>
                <th>Commission</th>
                <th>Slippage</th>
                <th>Net</th>
                <th>Equity</th>
                <th>Return</th>
                <th>DD (ITD)</th>
                <th className="pr-5">Sync</th>
              </tr>
            </thead>
            <tbody>
              {[...daily.data.days].reverse().map((d) => (
                <tr key={d.session_date} className="border-t border-slate-800">
                  <td className="px-5 py-1.5">{d.session_date}</td>
                  <td>{d.trades}</td>
                  <td className={signTone(d.gross_pnl)}>{usd(d.gross_pnl)}</td>
                  <td>{usd(d.commission)}</td>
                  <td>{usd(d.slippage_cost)}</td>
                  <td className={signTone(d.net_pnl)}>{usd(d.net_pnl)}</td>
                  <td>{usd(d.equity_end)}</td>
                  <td className={signTone(d.daily_return)}>{frac(d.daily_return)}</td>
                  <td>{frac(d.dd_itd)}</td>
                  <td className="pr-5">
                    <Chip value={sync.get(d.session_date)?.sync_status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      ) : null}
    </>
  );
}

function StrategyPage() {
  const { code } = useParams<{ code: string }>();
  const initialDate = useSearchParams().get("date") ?? undefined;
  const strategy = useQuery({ queryKey: ["hq-strategy", code], queryFn: () => getHqStrategy(code) });
  const daily = useQuery({ queryKey: ["hq-daily", code], queryFn: () => getHqDaily(code) });
  if (strategy.isLoading || strategy.isError) {
    return <QueryGate isLoading={strategy.isLoading} isError={strategy.isError} error={strategy.error} />;
  }
  const s = strategy.data!;
  const sessionDates = (daily.data?.sync ?? []).map((r) => r.session_date);
  return (
    <div>
      <Link href="/hq" className="text-sm text-slate-500 hover:text-slate-300">
        ← Futures HQ
      </Link>
      <div className="mb-6 mt-2 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{s.display_name}</h1>
          <p className="mt-1 text-sm text-slate-500">
            {s.strategy_code} · {s.instrument_root}
            {s.paper_start_date ? ` · paper since ${s.paper_start_date}` : ""}
            {s.notes ? ` · ${s.notes}` : ""}
          </p>
        </div>
        <div className="max-w-md"><Chip wrap value={s.overall_gate} /></div>
      </div>
      <KpiPanel code={code} dates={s.kpi_dates ?? []} />
      <DayPanel code={code} dates={sessionDates} initial={initialDate} />
      <History code={code} />
    </div>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<QueryGate isLoading isError={false} />}>
      <StrategyPage />
    </Suspense>
  );
}
