"use client";

import { useQuery } from "@tanstack/react-query";

import QueryGate from "@/components/query-gate";
import StagedBanner from "@/components/staged-banner";
import { getIbkrOptionsKpis, isNotFound } from "@/lib/api";
import { money } from "@/lib/format";
import type { OptionsKpiRow, OptionsKpiStage } from "@/lib/types";

function verdictClass(status: string) {
  if (status === "PASS") return "bg-emerald-600/20 text-emerald-300";
  if (status === "WARN") return "bg-amber-600/20 text-amber-200";
  if (status === "FAIL") return "bg-rose-600/20 text-rose-300";
  return "bg-slate-800 text-slate-400";
}

function formatValue(row: OptionsKpiRow) {
  if (row.value == null) return "—";
  if (row.key === "trade_count" || row.key === "years_covered" || row.key === "missed_fills") {
    return String(Math.round(row.value));
  }
  if (row.key === "ann_return" || row.key.includes("pct") || row.key === "max_dd" || row.key === "worst_month" || row.key === "cvar95" || row.key === "peak_margin" || row.key === "capital_util") {
    return `${row.value.toFixed(2)}%`;
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
            <th>Target</th>
            <th>Min</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {stage.kpis.map((row) => (
            <tr key={row.key} className="border-t border-slate-800 align-top">
              <td className="px-5 py-2">
                <div className="font-medium">
                  {row.label}
                  {row.critical ? <span className="ml-2 text-[10px] text-rose-300">CRIT</span> : null}
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
                {row.direction === "ge" ? "≥" : "≤"} {row.target}
              </td>
              <td className="text-slate-400">
                {row.direction === "ge" ? "≥" : "≤"} {row.min}
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
    queryKey: ["ibkr-options-kpis"],
    queryFn: getIbkrOptionsKpis,
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
        Options KPIs need a published dump. Run{" "}
        <code className="text-slate-200">waystone3 ibkr-seed-demo</code> locally or{" "}
        <code className="text-slate-200">ibkr-export</code> on the VM.
      </div>
    );
  }

  const score = data.stages.filter((s) => s.id !== "exec");
  const exec = data.stages.find((s) => s.id === "exec");

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Options KPIs</h1>
          <p className="mt-1 text-sm text-slate-500">
            Strategy 5 weekly paper scorecard. Computed from published option fills. Manual
            rows stay empty until greeks / ops inputs are added.
          </p>
        </div>
        <span className={`rounded px-3 py-1 text-sm ${verdictClass(data.overall)}`}>
          OVERALL {data.overall}
        </span>
      </div>
      {data.staged ? <StagedBanner week={data.staged_week} /> : null}
      <p className="mb-6 text-xs text-slate-500">
        As of {data.as_of ?? "—"} · {data.days} published day(s) · {data.trade_count} closed
        option trades
      </p>

      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4">
        <div className="card p-4">
          <div className="text-xs uppercase text-slate-500">Assumed NAV</div>
          <div className="mt-1 text-lg font-semibold">{money(data.assumptions.nav)}</div>
        </div>
        <div className="card p-4">
          <div className="text-xs uppercase text-slate-500">Contracts / trade</div>
          <div className="mt-1 text-lg font-semibold">{data.assumptions.contracts_per_trade}</div>
        </div>
        <div className="card p-4">
          <div className="text-xs uppercase text-slate-500">Option multiplier</div>
          <div className="mt-1 text-lg font-semibold">{data.assumptions.option_multiplier}</div>
        </div>
        <div className="card p-4">
          <div className="text-xs uppercase text-slate-500">Round-trip slippage</div>
          <div className="mt-1 text-lg font-semibold">
            {(data.assumptions.round_trip_slippage * 100).toFixed(1)}%
          </div>
        </div>
      </div>

      <div className="mb-6 flex flex-wrap gap-2">
        {score.map((s) => (
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
          <div className="mb-3 font-medium">Weekly options return</div>
          <div className="flex flex-wrap gap-3 text-sm">
            {data.weeks.map((w) => (
              <div
                key={w.week}
                className={`rounded px-3 py-2 ${
                  w.week === data.staged_iso_week
                    ? "bg-violet-700/40 ring-1 ring-violet-400/50"
                    : "bg-slate-800"
                }`}
              >
                <div className="text-xs text-slate-500">
                  {w.week}
                  {w.week === data.staged_iso_week ? " · staged" : ""}
                </div>
                <div className={w.return_pct >= 0 ? "text-emerald-300" : "text-rose-300"}>
                  {w.return_pct >= 0 ? "+" : ""}
                  {w.return_pct.toFixed(2)}%
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {score.map((s) => (
        <StageBlock key={s.id} stage={s} />
      ))}
      {exec ? <StageBlock stage={exec} /> : null}
    </div>
  );
}
