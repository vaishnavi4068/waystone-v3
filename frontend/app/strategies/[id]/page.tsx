"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";

import QueryGate from "@/components/query-gate";
import ResearchScorecardView, { GateChip } from "@/components/research-scorecard";
import { getStrategy, getStrategyRuns, getStrategyScorecard, openStrategyScorecardHtml } from "@/lib/api";

function EquityCurve({ points }: { points: number[] }) {
  if (points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const w = 640;
  const h = 200;
  const path = points
    .map((p, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h - ((p - min) / span) * h;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const up = points[points.length - 1] >= points[0];
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full">
      <path d={path} fill="none" stroke={up ? "#10b981" : "#f43f5e"} strokeWidth={2} />
    </svg>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="card p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-xl font-semibold">{value}</div>
    </div>
  );
}

function fmt(n: number | null | undefined, suffix = "") {
  if (n == null) return "—";
  return `${n}${suffix}`;
}

export default function Page() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const [date, setDate] = useState("");
  const [variant, setVariant] = useState("");
  const q = useQuery({
    queryKey: ["strategy", id, date, variant],
    queryFn: () => getStrategy(id, date || undefined, variant || undefined),
  });
  const runs = useQuery({
    queryKey: ["strategy-runs", id],
    queryFn: () => getStrategyRuns(id),
  });
  const score = useQuery({
    queryKey: ["strategy-scorecard", id, date, variant],
    queryFn: () => getStrategyScorecard(id, date || undefined, variant || undefined),
    enabled: Boolean(q.data?.latest),
  });
  const row = q.data;
  const stats = row?.latest?.stats;
  const variants = useMemo(() => {
    const fromRow = row?.variants ?? [];
    const fromRuns = (runs.data?.runs ?? []).map((r) => r.variant);
    return Array.from(new Set([...fromRow, ...fromRuns])).filter(Boolean);
  }, [row?.variants, runs.data?.runs]);

  return (
    <div>
      <Link href="/strategies" className="mb-4 inline-block text-sm text-slate-400 hover:text-slate-200">
        ← Strategies
      </Link>
      <QueryGate isLoading={q.isLoading} isError={q.isError} error={q.error}>
        {row && (
          <>
            <div className="mb-2 flex flex-wrap items-center gap-3">
              <h1 className="text-2xl font-semibold">{row.name}</h1>
              <span className="rounded bg-slate-800 px-2 py-0.5 text-xs text-slate-300">{row.book}</span>
              <GateChip overall={score.data?.overall ?? row.scorecard?.overall} />
            </div>
            <p className="mb-4 text-slate-400">{row.summary}</p>
            <div className="mb-6 text-sm text-slate-500">
              {row.instruments} · hold {row.holding_period}
            </div>
            <div className="card mb-6 p-5">
              <div className="mb-2 text-xs uppercase tracking-wide text-slate-500">Rule sketch</div>
              <p className="text-sm text-slate-300">{row.rule_sketch}</p>
            </div>
            <div className="mb-6 flex flex-wrap gap-4">
              {row.days.length > 0 && (
                <label className="block text-sm">
                  <span className="text-slate-500">Published as-of date</span>
                  <select
                    value={date || row.latest?.date || ""}
                    onChange={(e) => setDate(e.target.value)}
                    className="mt-1 w-full min-w-40 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2"
                  >
                    {row.days.map((d) => (
                      <option key={d} value={d}>
                        {d}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {variants.length > 0 && (
                <label className="block text-sm">
                  <span className="text-slate-500">Variant</span>
                  <select
                    value={variant || row.latest?.variant || ""}
                    onChange={(e) => setVariant(e.target.value)}
                    className="mt-1 w-full min-w-40 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2"
                  >
                    {variants.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {row.latest && (
                <button
                  type="button"
                  className="self-end rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-200 hover:border-slate-500"
                  onClick={() =>
                    openStrategyScorecardHtml(
                      id,
                      date || row.latest?.date,
                      variant || row.latest?.variant,
                    )
                  }
                >
                  Open HTML scorecard
                </button>
              )}
            </div>
            {row.latest ? (
              <>
                <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
                  <Metric label="Sharpe" value={fmt(stats?.sharpe)} />
                  <Metric label="CAGR" value={fmt(stats?.cagr_pct, "%")} />
                  <Metric label="Max DD" value={fmt(stats?.max_drawdown_pct, "%")} />
                  <Metric label="Trades" value={fmt(stats?.trade_count)} />
                  <Metric label="Win rate" value={fmt(stats?.win_rate_pct, "%")} />
                  <Metric label="Years" value={fmt(stats?.years)} />
                  <Metric label="As of" value={row.latest.date} />
                  <Metric label="Variant" value={row.latest.variant} />
                </div>
                <div className="card mb-8 p-5">
                  <div className="mb-3 text-xs uppercase tracking-wide text-slate-500">Equity</div>
                  <EquityCurve points={row.latest.equity} />
                </div>
                <h2 className="mb-3 text-lg font-semibold">Stage-gate scorecard</h2>
                <p className="mb-4 text-sm text-slate-400">
                  Same sequential gates as the options KPI dashboard. Stages 3–5 stay N/A until
                  attribution, incubation, or live logs exist.
                </p>
                {score.data ? (
                  <ResearchScorecardView card={score.data} />
                ) : (
                  <div className="text-sm text-slate-500">Building scorecard…</div>
                )}
              </>
            ) : (
              <div className="text-sm text-slate-500">No dated run published for this sleeve yet.</div>
            )}
          </>
        )}
      </QueryGate>
    </div>
  );
}
