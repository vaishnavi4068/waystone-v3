"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Chip, frac, kpiTarget, kpiValue, signTone, statusClass, usd } from "@/components/hq";
import { Section } from "@/components/hq-views";
import QueryGate from "@/components/query-gate";
import { getHqKpis } from "@/lib/api";
import type { HqKpi, HqKpis, HqStrategy } from "@/lib/types";

const WINDOWS = ["WEEK", "MTD", "ITD"] as const;
type Window = (typeof WINDOWS)[number];
const WINDOW_LABEL: Record<Window, string> = { WEEK: "This week", MTD: "Month to date", ITD: "Since paper start" };

interface Column {
  key: string;
  title: string;
  data: HqKpis;
  window: Window;
}

function Cell({ k }: { k: HqKpi | undefined }) {
  if (!k) return <span className="text-slate-600">—</span>;
  return (
    <span title={k.status ?? undefined} className={`inline-block rounded px-2 py-0.5 text-sm ${statusClass(k.status)}`}>
      {kpiValue(k)}
    </span>
  );
}

function Matrix({ columns, showTargets }: { columns: Column[]; showTargets: boolean }) {
  const ref = columns.find((c) => c.data.kpis.length)?.data;
  if (!ref) {
    return (
      <Section title="KPI scorecard">
        <div className="px-5 py-4 text-sm text-slate-500">No KPIs computed up to this date yet.</div>
      </Section>
    );
  }
  const lookup = columns.map(
    (c) => new Map(c.data.kpis.filter((k) => k.kpi_window === c.window).map((k) => [k.kpi_code, k])),
  );
  const defs = ref.kpis.filter((k) => k.kpi_window === columns[0].window && k.section !== "header");
  const sections = new Map<string, HqKpi[]>();
  for (const k of defs) sections.set(k.section_name, [...(sections.get(k.section_name) ?? []), k]);
  const cards = columns.map((c) => c.data.scorecard.find((s) => s.kpi_window === c.window));
  const span = columns.length + (showTargets ? 3 : 1);
  return (
    <table className="hq-table w-full text-sm">
      <thead className="text-left text-slate-500">
        <tr>
          <th className="px-5 py-2">KPI</th>
          {columns.map((c) => (
            <th key={c.key}>{c.title}</th>
          ))}
          {showTargets ? (
            <>
              <th>Green</th>
              <th className="pr-5">Amber</th>
            </>
          ) : null}
        </tr>
      </thead>
      <tbody>
        {[
          ["Window", (i: number) => (cards[i] ? `${cards[i]!.window_start} → ${cards[i]!.window_end}` : "—")],
          ["Trading days / trades", (i: number) => (cards[i] ? `${cards[i]!.trading_days} / ${cards[i]!.trades}` : "—")],
          [
            "Net P&L",
            (i: number) => <span className={signTone(cards[i]?.net_pnl)}>{usd(cards[i]?.net_pnl)}</span>,
          ],
          [
            "Return",
            (i: number) => <span className={signTone(cards[i]?.return_pct)}>{frac(cards[i]?.return_pct)}</span>,
          ],
          ["Equity", (i: number) => usd(cards[i]?.equity_end)],
          [
            "KPIs green / amber / red",
            (i: number) =>
              cards[i] ? (
                <span>
                  <span className="text-emerald-300">{cards[i]!.green_count}</span> /{" "}
                  <span className="text-amber-200">{cards[i]!.amber_count}</span> /{" "}
                  <span className="text-rose-300">{cards[i]!.red_count}</span>
                </span>
              ) : (
                "—"
              ),
          ],
        ].map(([label, render]) => (
          <tr key={label as string} className="border-t border-slate-800">
            <td className="px-5 py-1.5 text-slate-400">{label as string}</td>
            {columns.map((c, i) => (
              <td key={c.key}>{(render as (i: number) => React.ReactNode)(i)}</td>
            ))}
            {showTargets ? <td colSpan={2} /> : null}
          </tr>
        ))}
        <tr className="border-t border-slate-800 align-top">
          <td className="px-5 py-2 text-slate-400">Verdict</td>
          {columns.map((c, i) => (
            <td key={c.key} className="wrap-cell max-w-xs py-2">
              <Chip wrap value={cards[i]?.overall_gate} />
            </td>
          ))}
          {showTargets ? <td colSpan={2} /> : null}
        </tr>
      </tbody>
      {[...sections.entries()].map(([name, rows]) => (
        <tbody key={name}>
          <tr className="border-t border-slate-700 bg-slate-900/50">
            <td colSpan={span} className="px-5 py-1.5 text-xs uppercase tracking-wide text-slate-400">
              {name}
            </td>
          </tr>
          {rows.map((k) => (
            <tr key={k.kpi_code} className="border-t border-slate-800 align-top">
              <td className="wrap-cell px-5 py-2">
                <div className="font-medium">{k.label}</div>
                {k.description ? <div className="mt-0.5 max-w-md text-xs text-slate-500">{k.description}</div> : null}
              </td>
              {columns.map((c, i) => (
                <td key={c.key} className="py-2">
                  <Cell k={lookup[i].get(k.kpi_code)} />
                </td>
              ))}
              {showTargets ? (
                <>
                  <td className="whitespace-nowrap py-2 text-slate-400">{kpiTarget(k, k.green_at)}</td>
                  <td className="whitespace-nowrap py-2 pr-5 text-slate-400">{kpiTarget(k, k.amber_at)}</td>
                </>
              ) : null}
            </tr>
          ))}
        </tbody>
      ))}
    </table>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
      <Chip value="GREEN" /> meets target <Chip value="AMBER" /> watch <Chip value="RED" /> fails <Chip value="NA" /> not
      enough data
    </div>
  );
}

export function StrategyKpis({ strategy, date }: { strategy: HqStrategy; date: string }) {
  const q = useQuery({
    queryKey: ["hq-kpis", strategy.strategy_code, date],
    queryFn: () => getHqKpis(strategy.strategy_code, date),
  });
  if (q.isLoading || q.isError) return <QueryGate isLoading={q.isLoading} isError={q.isError} error={q.error} />;
  const data = q.data!;
  return (
    <Section
      title={`${strategy.display_name} — KPI scorecard`}
      subtitle={data.as_of_date ? `As of ${data.as_of_date} · targets from the futures KPI workbook` : undefined}
      right={<Legend />}
    >
      <Matrix
        showTargets
        columns={WINDOWS.map((w) => ({ key: w, title: WINDOW_LABEL[w], data, window: w }))}
      />
    </Section>
  );
}

export function AllKpis({ strategies, date }: { strategies: HqStrategy[]; date: string }) {
  const [win, setWin] = useState<Window>("ITD");
  const results = useQueries({
    queries: strategies.map((s) => ({
      queryKey: ["hq-kpis", s.strategy_code, date],
      queryFn: () => getHqKpis(s.strategy_code, date),
    })),
  });
  if (results.some((r) => r.isLoading)) return <QueryGate isLoading isError={false} />;
  const columns = strategies
    .map((s, i) => ({ s, data: results[i].data }))
    .filter((r): r is { s: HqStrategy; data: HqKpis } => Boolean(r.data))
    .map(({ s, data }) => ({ key: s.strategy_code, title: s.display_name, data, window: win }));
  return (
    <Section
      title="All strategies — KPI comparison"
      subtitle={`${WINDOW_LABEL[win]} as of ${date} · pick a strategy above to see every window with targets`}
      right={
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex gap-1">
            {WINDOWS.map((w) => (
              <button
                key={w}
                onClick={() => setWin(w)}
                className={`rounded px-3 py-1 text-sm ${win === w ? "bg-emerald-600/20 text-emerald-300" : "text-slate-400 hover:bg-slate-800"}`}
              >
                {WINDOW_LABEL[w]}
              </button>
            ))}
          </div>
          <Legend />
        </div>
      }
    >
      <Matrix showTargets columns={columns} />
    </Section>
  );
}
