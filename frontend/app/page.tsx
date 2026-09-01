"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { getAccount } from "@/lib/api";
import { money } from "@/lib/format";

function Stat({
  label,
  value,
  className = "",
}: {
  label: string;
  value: string;
  className?: string;
}) {
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

  const ibkr = data.broker === "ibkr";

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">{ibkr ? "IBKR account" : "Shared Account"}</h1>
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

      {ibkr && data.today_published === false && (
        <div className="mb-6 rounded-lg border border-amber-700/40 bg-amber-950/30 px-4 py-3 text-sm text-amber-200">
          Today is not published yet. Showing
          {data.report_date ? ` ${data.report_date}` : " the last dump"}. Run{" "}
          <code className="text-amber-100">waystone3 ibkr-export</code> on the VM.
        </div>
      )}

      <div className="mb-8 grid grid-cols-1 gap-4 md:grid-cols-3">
        <Stat label={ibkr ? "NLV" : "Equity"} value={money(data.equity)} />
        <Stat label="Cash" value={money(data.cash)} />
        <Stat label="Buying power" value={money(data.buying_power)} />
      </div>

      {ibkr && (
        <div className="mb-8 grid grid-cols-1 gap-4 md:grid-cols-3">
          <Stat label="Excess liquidity" value={money(data.excess_liquidity ?? 0)} />
          <Stat label="Maint. margin" value={money(data.maint_margin ?? 0)} />
          <Stat
            label="As of"
            value={data.as_of ? new Date(data.as_of).toLocaleString() : "—"}
            className="text-lg text-slate-200"
          />
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="card p-5">
          <div className="mb-3 font-medium">{ibkr ? "Report" : "Shared strategy"}</div>
          {ibkr ? (
            <div className="space-y-2 text-sm">
              <div className="text-slate-400">
                Snapshot date: <span className="text-slate-200">{data.report_date ?? "none"}</span>
              </div>
              <p className="text-slate-500">
                Day P&amp;L, futures vs options, and fills are on{" "}
                <Link href="/ibkr" className="text-emerald-300 hover:underline">
                  Daily
                </Link>
                .
              </p>
            </div>
          ) : data.strategy ? (
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
            {ibkr
              ? "Read-only snapshot from GCS. Live fills come from the VM dump."
              : "Everyone here operates this one account. Actions are attributed to the member who runs them — see Activity."}
          </p>
        </div>
      </div>
    </div>
  );
}
