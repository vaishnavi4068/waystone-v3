"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { runBacktest } from "@/lib/api";
import { pct, tone } from "@/lib/format";

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

function Metric({ label, value, className = "" }: { label: string; value: string; className?: string }) {
  return (
    <div className="card p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${className}`}>{value}</div>
    </div>
  );
}

export default function Page() {
  const [symbols, setSymbols] = useState("SPY");
  const [start, setStart] = useState("2023-01-01");
  const [end, setEnd] = useState("2024-01-01");
  const [weights, setWeights] = useState("ma_crossover:0.5,price_action:0.5");

  const mut = useMutation({
    mutationFn: () => runBacktest({ symbols, start, end, weights }),
  });

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold">Backtests</h1>
      <div className="card mb-6 grid grid-cols-1 gap-3 p-5 md:grid-cols-2">
        <label className="text-sm">
          <span className="text-slate-500">Symbols</span>
          <input
            value={symbols}
            onChange={(e) => setSymbols(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2"
          />
        </label>
        <label className="text-sm">
          <span className="text-slate-500">Weights (name:weight,…)</span>
          <input
            value={weights}
            onChange={(e) => setWeights(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2"
          />
        </label>
        <label className="text-sm">
          <span className="text-slate-500">Start</span>
          <input
            value={start}
            onChange={(e) => setStart(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2"
          />
        </label>
        <label className="text-sm">
          <span className="text-slate-500">End</span>
          <input
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2"
          />
        </label>
        <div>
          <button
            onClick={() => mut.mutate()}
            disabled={mut.isPending}
            className="rounded-lg bg-emerald-600 px-5 py-2 font-medium hover:bg-emerald-500 disabled:opacity-50"
          >
            {mut.isPending ? "Running…" : "Run backtest"}
          </button>
        </div>
      </div>

      {mut.isError && (
        <div className="card border-rose-700 p-4 text-sm text-rose-300">
          Backtest failed — check symbols, dates, and weight names.
        </div>
      )}

      {mut.data && (
        <div>
          <div className="mb-4 grid grid-cols-2 gap-4 md:grid-cols-4">
            <Metric
              label="Total return"
              value={pct(mut.data.metrics.total_return_pct)}
              className={tone(mut.data.metrics.total_return_pct)}
            />
            <Metric label="Max drawdown" value={`${mut.data.metrics.max_drawdown_pct.toFixed(2)}%`} />
            <Metric label="Win rate" value={`${mut.data.metrics.win_rate_pct.toFixed(0)}%`} />
            <Metric label="Trades" value={String(mut.data.metrics.trades)} />
          </div>
          <div className="card p-5">
            <div className="mb-2 text-sm text-slate-400">Equity curve</div>
            <EquityCurve points={mut.data.equity} />
          </div>
        </div>
      )}
    </div>
  );
}
