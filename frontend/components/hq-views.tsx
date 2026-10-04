"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Chip, frac, kpiTarget, kpiValue, num, signTone, stamp, usd } from "@/components/hq";
import QueryGate from "@/components/query-gate";
import { getHqDaily, getHqKpis, getHqReturns, getHqStatus } from "@/lib/api";
import type { HqKpi, HqStrategy } from "@/lib/types";

const WINDOWS = ["ITD", "MTD", "WEEK"] as const;

export function Section({
  title,
  subtitle,
  right,
  children,
}: {
  title: string;
  subtitle?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="card mb-6 overflow-x-auto">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-5 py-3">
        <div>
          <div className="font-medium">{title}</div>
          {subtitle ? <div className="text-xs text-slate-500">{subtitle}</div> : null}
        </div>
        {right}
      </div>
      {children}
    </div>
  );
}

function Stat({ label, value, tone, sub }: { label: string; value: string; tone?: string; sub?: string }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{label}</div>
      <div className={`text-lg font-semibold ${tone ?? ""}`}>{value}</div>
      {sub ? <div className="text-xs text-slate-500">{sub}</div> : null}
    </div>
  );
}

export function StrategyCards({ strategies, onPick }: { strategies: HqStrategy[]; onPick: (code: string) => void }) {
  return (
    <Section title="Since paper start" subtitle="Inception-to-date, as of the latest loaded session">
      <div className="grid gap-4 p-5 md:grid-cols-3">
        {strategies
          .filter((s) => s.asset_class === "future")
          .map((s) => (
            <button
              key={s.strategy_code}
              onClick={() => onPick(s.strategy_code)}
              className="rounded-lg border border-slate-800 p-4 text-left hover:border-slate-600"
            >
              <div className="font-medium">{s.display_name}</div>
              <div className="mb-3 text-xs text-slate-500">
                paper since {s.paper_start_date ?? "—"} · {s.trading_days ?? 0} days · {s.trades ?? 0} trades
              </div>
              <div className="grid grid-cols-3 gap-2">
                <Stat label="Net P&L" value={usd(s.net_pnl)} tone={signTone(s.net_pnl)} />
                <Stat label="Return" value={frac(s.return_pct)} tone={signTone(s.return_pct)} />
                <Stat label="Equity" value={usd(s.equity_end)} />
              </div>
              <div className="mt-3">
                <Chip wrap value={s.overall_gate} />
              </div>
            </button>
          ))}
      </div>
    </Section>
  );
}

/* ----------------------------------------------------------- one strategy, one date */

export function KpiPanel({ code, date }: { code: string; date: string }) {
  const [win, setWin] = useState<(typeof WINDOWS)[number]>("ITD");
  const kpis = useQuery({ queryKey: ["hq-kpis", code, date], queryFn: () => getHqKpis(code, date) });
  if (kpis.isLoading || kpis.isError) {
    return <QueryGate isLoading={kpis.isLoading} isError={kpis.isError} error={kpis.error} />;
  }
  const data = kpis.data!;
  if (!data.as_of_date) {
    return (
      <Section title="KPI scorecard">
        <div className="px-5 py-4 text-sm text-slate-500">No KPIs up to {date}.</div>
      </Section>
    );
  }
  const sections = new Map<string, HqKpi[]>();
  for (const k of data.kpis.filter((k) => k.kpi_window === win)) {
    sections.set(k.section_name, [...(sections.get(k.section_name) ?? []), k]);
  }
  const labels: Record<string, string> = { ITD: "Since start", MTD: "Month to date", WEEK: "This week" };
  return (
    <Section
      title="KPI scorecard"
      subtitle={`Workbook KPIs as of ${data.as_of_date}`}
      right={
        <div className="flex gap-1">
          {WINDOWS.map((w) => (
            <button
              key={w}
              onClick={() => setWin(w)}
              className={`rounded px-3 py-1 text-sm ${win === w ? "bg-emerald-600/20 text-emerald-300" : "text-slate-400 hover:bg-slate-800"}`}
            >
              {labels[w]}
            </button>
          ))}
        </div>
      }
    >
      {(() => {
        const c = data.scorecard.find((s) => s.kpi_window === win);
        return c ? (
          <div className="flex flex-wrap items-center gap-6 border-b border-slate-800 px-5 py-4">
            <Stat label="Net P&L" value={usd(c.net_pnl)} tone={signTone(c.net_pnl)} />
            <Stat label="Return" value={frac(c.return_pct)} tone={signTone(c.return_pct)} />
            <Stat label="Days / trades" value={`${c.trading_days ?? "—"} / ${c.trades ?? "—"}`} />
            <Stat label="Window" value={`${c.window_start ?? "—"} → ${c.window_end ?? "—"}`} />
            <div className="max-w-md">
              <div className="mb-1 text-xs text-slate-500">Verdict</div>
              <Chip wrap value={c.overall_gate} />
            </div>
          </div>
        ) : null;
      })()}
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
        {[...sections.entries()]
          .filter(([, rows]) => rows[0]?.section !== "header")
          .map(([name, rows]) => (
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
                  <td><Chip value={k.status} /></td>
                  <td className="text-slate-400">{kpiTarget(k, k.green_at)}</td>
                  <td className="pr-5 text-slate-400">{kpiTarget(k, k.amber_at)}</td>
                </tr>
              ))}
            </tbody>
          ))}
      </table>
    </Section>
  );
}

export function History({ code, date, onPickDate }: { code: string; date: string; onPickDate: (d: string) => void }) {
  const daily = useQuery({ queryKey: ["hq-daily", code], queryFn: () => getHqDaily(code) });
  const returns = useQuery({ queryKey: ["hq-returns", code], queryFn: () => getHqReturns(code) });
  const sync = new Map((daily.data?.sync ?? []).map((s) => [s.session_date, s]));
  return (
    <>
      {daily.data ? (
        <Section title="Daily P&L" subtitle="Every session since paper start · click a row to open that date">
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
                <th>Drawdown</th>
                <th className="pr-5">Result</th>
              </tr>
            </thead>
            <tbody>
              {[...daily.data.days].reverse().map((d) => (
                <tr
                  key={d.session_date}
                  onClick={() => onPickDate(d.session_date)}
                  className={`cursor-pointer border-t border-slate-800 hover:bg-slate-800/40 ${
                    d.session_date === date ? "bg-emerald-600/10" : ""
                  }`}
                >
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
      {returns.data ? (
        <div className="grid gap-x-4 lg:grid-cols-2">
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
          </Section>
        </div>
      ) : null}
    </>
  );
}

export function LoaderStatus() {
  const status = useQuery({ queryKey: ["hq-status"], queryFn: getHqStatus });
  const loads = status.data?.loads ?? [];
  if (loads.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-4 text-xs text-slate-500">
      {loads.map((l) => (
        <span key={l.job} title={l.error ?? undefined} className="flex items-center gap-1">
          last {l.job} load <Chip value={l.status} /> {stamp(l.finished_at ?? l.started_at)}
        </span>
      ))}
    </div>
  );
}
