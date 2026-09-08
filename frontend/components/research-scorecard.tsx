"use client";

import type { ResearchScorecard, ResearchScorecardKpi, ResearchScorecardStage } from "@/lib/types";

function verdictClass(status: string) {
  const s = (status || "").toUpperCase();
  if (s === "PASS") return "bg-emerald-600/20 text-emerald-300";
  if (s === "WARN") return "bg-amber-600/20 text-amber-200";
  if (s === "FAIL") return "bg-rose-600/20 text-rose-300";
  return "bg-slate-800 text-slate-400";
}

function formatValue(row: ResearchScorecardKpi) {
  if (row.value == null || row.value === "") return "—";
  if (typeof row.value === "boolean") return row.value ? "yes" : "no";
  if (typeof row.value === "number") {
    if (row.id === "ntrades" || row.id === "regimes") return String(Math.round(row.value));
    if (row.unit === "USD") return `$${row.value.toFixed(2)}`;
    return Number.isInteger(row.value) ? String(row.value) : row.value.toFixed(3);
  }
  return String(row.value);
}

function StageBlock({ stage }: { stage: ResearchScorecardStage }) {
  const kpis = stage.kpis ?? [];
  return (
    <div className="card mb-6 overflow-hidden">
      <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
        <div>
          <div className="font-medium">{stage.name}</div>
          {stage.desc ? <div className="mt-1 max-w-3xl text-xs text-slate-500">{stage.desc}</div> : null}
        </div>
        <div className="flex items-center gap-3 text-sm">
          <span className="text-slate-500">
            {stage.filled}/{stage.total}
          </span>
          <span className={`rounded px-2 py-0.5 text-xs ${verdictClass(stage.verdict)}`}>{stage.verdict}</span>
        </div>
      </div>
      {kpis.length > 0 && (
        <table className="w-full text-sm">
          <thead className="bg-slate-900/60 text-left text-slate-500">
            <tr>
              <th className="px-5 py-2">KPI</th>
              <th>Value</th>
              <th>Status</th>
              <th>Target</th>
              <th>Warn</th>
            </tr>
          </thead>
          <tbody>
            {kpis.map((row) => (
              <tr key={row.id} className="border-t border-slate-800 align-top">
                <td className="px-5 py-2">
                  <div className="font-medium">
                    {row.name}
                    {row.critical ? <span className="ml-2 text-[10px] text-rose-300">CRIT</span> : null}
                  </div>
                  <div className="mt-1 max-w-md text-xs text-slate-500">{row.definition}</div>
                </td>
                <td className="whitespace-nowrap">{formatValue(row)}</td>
                <td>
                  <span className={`rounded px-2 py-0.5 text-xs ${verdictClass(row.status)}`}>
                    {row.status.toUpperCase()}
                  </span>
                </td>
                <td className="text-slate-400">
                  {row.type === "gte" || row.type === "lte"
                    ? `${row.type === "gte" ? "≥" : "≤"} ${row.pass ?? "—"}`
                    : "—"}
                </td>
                <td className="text-slate-400">
                  {row.type === "gte" || row.type === "lte"
                    ? `${row.type === "gte" ? "≥" : "≤"} ${row.warn ?? "—"}`
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function money(value: number | string | null | undefined) {
  if (value == null || value === "") return "—";
  const num = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(num)) return "—";
  return `$${num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export default function ResearchScorecardView({ card }: { card: ResearchScorecard }) {
  const avgMonth =
    card.avg_monthly_net_usd ??
    (typeof card.banner?.avg_monthly_net_usd === "number" ? card.banner.avg_monthly_net_usd : null);
  const trades = card.trade_details ?? [];
  const months = card.pnl_by_month ?? [];

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <span className={`rounded px-2 py-0.5 text-xs ${verdictClass(card.overall)}`}>
          Overall {card.overall}
        </span>
        {avgMonth != null ? (
          <span className="rounded bg-slate-800 px-2 py-0.5 text-xs text-emerald-200">
            Avg monthly net {money(avgMonth)}
          </span>
        ) : null}
        {card.date ? <span className="text-xs text-slate-500">as of {card.date}</span> : null}
        {card.variant ? <span className="text-xs text-slate-500">variant {card.variant}</span> : null}
        {card.window?.start ? (
          <span className="text-xs text-slate-500">
            {card.window.start} → {card.window.end}
          </span>
        ) : null}
      </div>
      {(card.notes ?? []).length > 0 && (
        <ul className="card mb-6 list-disc px-8 py-4 text-sm text-amber-200">
          {card.notes!.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      {months.length > 0 && (
        <div className="card mb-6 overflow-hidden">
          <div className="border-b border-slate-800 px-5 py-3 font-medium">P&L by month</div>
          <table className="w-full text-sm">
            <thead className="bg-slate-900/60 text-left text-slate-500">
              <tr>
                <th className="px-5 py-2">Month</th>
                <th>Net P&L</th>
              </tr>
            </thead>
            <tbody>
              {months.map((row) => (
                <tr key={row.month} className="border-t border-slate-800">
                  <td className="px-5 py-2">{row.month}</td>
                  <td>{money(row.pnl_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {trades.length > 0 && (
        <div className="card mb-6 overflow-hidden">
          <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
            <div className="font-medium">Trade log</div>
            <div className="text-xs text-slate-500">
              {card.trade_count_total ?? trades.length} closed trades
              {card.trade_details_truncated ? ` · showing first ${trades.length}` : ""}
            </div>
          </div>
          <div className="max-h-[420px] overflow-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-slate-900/95 text-left text-slate-500">
                <tr>
                  <th className="px-5 py-2">Entry</th>
                  <th>Exit</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Qty</th>
                  <th>Entry px</th>
                  <th>Exit px</th>
                  <th>P&L</th>
                  <th>Hold</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t, i) => (
                  <tr key={`${t.symbol}-${t.entry_time}-${i}`} className="border-t border-slate-800">
                    <td className="px-5 py-2 whitespace-nowrap">{t.entry_time || "—"}</td>
                    <td className="whitespace-nowrap">{t.exit_time || "—"}</td>
                    <td className="font-medium">{t.symbol || "—"}</td>
                    <td>{t.side || "—"}</td>
                    <td>{t.qty ?? "—"}</td>
                    <td>{t.entry_price ?? "—"}</td>
                    <td>{t.exit_price ?? "—"}</td>
                    <td>{money(t.pnl)}</td>
                    <td>{t.hold_days ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {card.stages.map((stage) => (
        <StageBlock key={stage.id} stage={stage} />
      ))}
    </div>
  );
}

export function GateChip({ overall }: { overall?: string | null }) {
  if (!overall) return null;
  return <span className={`rounded px-2 py-0.5 text-xs ${verdictClass(overall)}`}>{overall}</span>;
}
