"use client";

import { useQuery } from "@tanstack/react-query";

import QueryGate from "@/components/query-gate";
import { getIbkrFuturesKpis, isNotFound } from "@/lib/api";
import { money } from "@/lib/format";
import type { OptionsKpiRow, OptionsKpiStage } from "@/lib/types";

function verdictClass(status: string) {
  if (status === "GREEN" || status === "PASS") return "bg-emerald-600/20 text-emerald-300";
  if (status === "AMBER" || status === "WARN" || status === "MARGINAL") {
    return "bg-amber-600/20 text-amber-200";
  }
  if (status === "RED" || status === "FAIL" || status === "REJECTED" || status === "CONDITIONAL") {
    return "bg-rose-600/20 text-rose-300";
  }
  return "bg-slate-800 text-slate-400";
}

function formatTarget(value: number, key: string) {
  if (
    key === "max_dd" ||
    key === "ann_vol" ||
    key === "cvar95" ||
    key === "win_rate" ||
    key === "time_in_market" ||
    key === "cost_drag" ||
    key === "margin_to_equity" ||
    key === "roll_cost_drag"
  ) {
    return `${(value * 100).toFixed(0)}%`;
  }
  if (key === "capacity") return money(value);
  if (key === "trade_count" || key === "n_trials" || key === "max_dd_months") {
    return String(Math.round(value));
  }
  return String(value);
}

function formatValue(row: OptionsKpiRow) {
  if (row.value == null) return "—";
  if (row.key === "trade_count" || row.key === "n_trials") return String(Math.round(row.value));
  if (row.key === "capacity") return money(row.value);
  if (
    row.key === "max_dd" ||
    row.key === "ann_vol" ||
    row.key === "cvar95" ||
    row.key === "win_rate" ||
    row.key === "time_in_market" ||
    row.key === "cost_drag" ||
    row.key === "margin_to_equity" ||
    row.key === "roll_cost_drag"
  ) {
    return `${(row.value * 100).toFixed(2)}%`;
  }
  return row.value.toFixed(2);
}

function StageBlock({ stage }: { stage: OptionsKpiStage }) {
  return (
    <div className="card mb-6 overflow-hidden">
      <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
        <div className="font-medium">{stage.name}</div>
        <div className="flex items-center gap-3 text-sm">
          <span className="text-slate-500">
            {stage.filled}/{stage.total}
          </span>
          <span className={`rounded px-2 py-0.5 text-xs ${verdictClass(stage.verdict)}`}>
            {stage.verdict}
          </span>
        </div>
      </div>
      <table className="w-full text-sm">
        <thead className="bg-slate-900/60 text-left text-slate-500">
          <tr>
            <th className="px-5 py-2">KPI</th>
            <th>Value</th>
            <th>Status</th>
            <th>Green</th>
            <th>Amber</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {stage.kpis.map((row) => (
            <tr key={row.key} className="border-t border-slate-800 align-top">
              <td className="px-5 py-2">
                <div className="font-medium">
                  {row.label}
                  {row.critical ? <span className="ml-2 text-[10px] text-rose-300">T0</span> : null}
                </div>
                <div className="mt-1 max-w-md text-xs text-slate-500">{row.definition}</div>
              </td>
              <td className="whitespace-nowrap">{formatValue(row)}</td>
              <td>
                <span className={`rounded px-2 py-0.5 text-xs ${verdictClass(row.status)}`}>
                  {row.status}
                </span>
              </td>
              <td className="text-slate-400">
                {row.direction === "ge" ? "≥" : "≤"} {formatTarget(row.target, row.key)}
              </td>
              <td className="text-slate-400">
                {row.direction === "ge" ? "≥" : "≤"} {formatTarget(row.min, row.key)}
              </td>
              <td className="text-slate-500">{row.source}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Page() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["ibkr-futures-kpis"],
    queryFn: getIbkrFuturesKpis,
  });

  if (isLoading) {
    return <QueryGate isLoading isError={false} />;
  }
  if (isError && !isNotFound(error)) {
    return <QueryGate isLoading={false} isError error={error} />;
  }
  if (isError || !data) {
    return (
      <div className="card p-5 text-sm text-slate-400">
        Futures KPIs need a published dump. Run{" "}
        <code className="text-slate-200">waystone3 ibkr-seed-demo</code> locally or{" "}
        <code className="text-slate-200">ibkr-export</code> on the VM.
      </div>
    );
  }

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Futures KPIs</h1>
          <p className="mt-1 text-sm text-slate-500">
            NQ scorecard (v5.1). Tiers 0–4 from published futures fills. Evaluate on
            out-of-sample data only — KPIs are gates, not dials. Manual rows stay empty
            until trial counts / benchmark / ops inputs are added.
          </p>
        </div>
        <span className={`rounded px-3 py-1 text-sm ${verdictClass(data.overall)}`}>
          OVERALL {data.overall}
        </span>
      </div>
      <p className="mb-6 text-xs text-slate-500">
        {data.instrument} · As of {data.as_of ?? "—"} · {data.days} published day(s) ·{" "}
        {data.trade_count} closed futures trades
      </p>

      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-3">
        <div className="card p-4">
          <div className="text-xs uppercase text-slate-500">Assumed NAV</div>
          <div className="mt-1 text-lg font-semibold">{money(data.assumptions.nav)}</div>
        </div>
        <div className="card p-4">
          <div className="text-xs uppercase text-slate-500">Contracts / trade</div>
          <div className="mt-1 text-lg font-semibold">{data.assumptions.contracts_per_trade}</div>
        </div>
        <div className="card p-4">
          <div className="text-xs uppercase text-slate-500">Point value</div>
          <div className="mt-1 text-lg font-semibold">${data.assumptions.point_value}/pt</div>
        </div>
      </div>

      <div className="mb-6 flex flex-wrap gap-2">
        {data.stages.map((s) => (
          <div key={s.id} className="card px-4 py-3 text-sm">
            <div className="text-slate-400">{s.name.replace(/ — .*/, "")}</div>
            <div className="mt-1 flex items-center gap-2">
              <span className={`rounded px-2 py-0.5 text-xs ${verdictClass(s.verdict)}`}>
                {s.verdict}
              </span>
              <span className="text-slate-500">
                {s.filled}/{s.total}
              </span>
            </div>
          </div>
        ))}
      </div>

      {data.weeks.length > 0 && (
        <div className="card mb-6 overflow-x-auto p-5">
          <div className="mb-3 font-medium">Weekly futures return</div>
          <div className="flex flex-wrap gap-3 text-sm">
            {data.weeks.map((w) => (
              <div key={w.week} className="rounded bg-slate-800 px-3 py-2">
                <div className="text-xs text-slate-500">{w.week}</div>
                <div className={w.return_pct >= 0 ? "text-emerald-300" : "text-rose-300"}>
                  {w.return_pct >= 0 ? "+" : ""}
                  {w.return_pct.toFixed(2)}%
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {data.stages.map((s) => (
        <StageBlock key={s.id} stage={s} />
      ))}
    </div>
  );
}
