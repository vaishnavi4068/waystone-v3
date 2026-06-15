"use client";

import { useQuery } from "@tanstack/react-query";

import { getAccount } from "@/lib/api";
import { money } from "@/lib/format";

function Stat({ label, value, className = "" }: { label: string; value: string; className?: string }) {
  return (
    <div className="card p-5">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-2 text-2xl font-semibold ${className}`}>{value}</div>
    </div>
  );
}

export default function Page() {
  const { data, isLoading } = useQuery({ queryKey: ["account"], queryFn: getAccount });
  if (isLoading || !data) return <div className="text-slate-400">Loading…</div>;

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Shared Account</h1>
        <div className="text-sm text-slate-400">
          {data.broker} ·{" "}
          <span className={data.is_paper ? "text-amber-400" : "text-rose-400"}>
            {data.is_paper ? "PAPER" : "LIVE"}
          </span>{" "}
          ·{" "}
          <span className={data.trading_enabled ? "text-emerald-400" : "text-rose-400"}>
            {data.trading_enabled ? "trading on" : "HALTED"}
          </span>
        </div>
      </div>

      <div className="mb-8 grid grid-cols-1 gap-4 md:grid-cols-3">
        <Stat label="Equity" value={money(data.equity)} />
        <Stat label="Cash" value={money(data.cash)} />
        <Stat label="Buying power" value={money(data.buying_power)} />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="card p-5">
          <div className="mb-3 font-medium">Shared strategy</div>
          {data.strategy ? (
            <div className="space-y-3 text-sm">
              <div>
                <div className="text-slate-500">Weights</div>
                <div className="mt-1 flex flex-wrap gap-2">
                  {Object.entries(data.strategy.weights).map(([k, v]) => (
                    <span key={k} className="rounded bg-slate-800 px-2 py-1">
                      {k}: {v}
                    </span>
                  ))}
                </div>
              </div>
              <div>
                <div className="text-slate-500">Watchlist</div>
                <div className="mt-1">{data.strategy.watchlist.join(", ")}</div>
              </div>
              <div className="flex gap-6">
                <div>
                  <div className="text-slate-500">Bullish ≥</div>
                  <div>{data.strategy.bullish_threshold}</div>
                </div>
                <div>
                  <div className="text-slate-500">Bearish ≤</div>
                  <div>{data.strategy.bearish_threshold}</div>
                </div>
                <div>
                  <div className="text-slate-500">Notional</div>
                  <div>{money(data.strategy.notional_per_trade)}</div>
                </div>
              </div>
            </div>
          ) : (
            <div className="text-sm text-slate-500">
              No shared strategy yet — set one from Claude (&quot;set strategy …&quot;).
            </div>
          )}
        </div>

        <div className="card p-5">
          <div className="mb-3 font-medium">Team</div>
          <div className="flex flex-wrap gap-2 text-sm">
            {data.team.map((m) => (
              <span
                key={m}
                className={`rounded px-2 py-1 ${m === data.you ? "bg-emerald-600/20 text-emerald-300" : "bg-slate-800"}`}
              >
                {m}
                {m === data.you ? " (you)" : ""}
              </span>
            ))}
          </div>
          <p className="mt-4 text-xs text-slate-500">
            Everyone here operates this one account. Actions are attributed to the member
            who runs them — see Activity.
          </p>
        </div>
      </div>
    </div>
  );
}
