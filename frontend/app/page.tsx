"use client";

import { useQuery } from "@tanstack/react-query";

import { getMe } from "@/lib/api";
import { money, pct, tone } from "@/lib/format";

function Stat({ label, value, className = "" }: { label: string; value: string; className?: string }) {
  return (
    <div className="card p-5">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-2 text-2xl font-semibold ${className}`}>{value}</div>
    </div>
  );
}

export default function Page() {
  const { data, isLoading } = useQuery({ queryKey: ["me"], queryFn: getMe });

  if (isLoading || !data) return <div className="text-slate-400">Loading…</div>;

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold">My Dashboard · {data.player}</h1>

      <div className="mb-8 grid grid-cols-1 gap-4 md:grid-cols-4">
        <Stat label="Equity" value={money(data.account.equity)} />
        <Stat label="Cash" value={money(data.account.cash)} />
        <Stat
          label="Return"
          value={pct(data.account.return_pct)}
          className={tone(data.account.return_pct)}
        />
        <Stat label="Rank" value={data.rank ? `#${data.rank}` : "—"} />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="card p-5">
          <div className="mb-3 font-medium">Positions · {data.cycles_run} cycles run</div>
          {data.positions.length === 0 ? (
            <div className="text-sm text-slate-500">No open positions.</div>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-slate-500">
                <tr>
                  <th className="py-1">Symbol</th>
                  <th>Qty</th>
                  <th>Entry</th>
                  <th>Mark</th>
                  <th>Unrealized</th>
                </tr>
              </thead>
              <tbody>
                {data.positions.map((p) => (
                  <tr key={p.symbol} className="border-t border-slate-800">
                    <td className="py-1.5 font-medium">{p.symbol}</td>
                    <td>{p.qty}</td>
                    <td>{money(p.avg_entry_price)}</td>
                    <td>{p.market_price != null ? money(p.market_price) : "—"}</td>
                    <td className={p.unrealized_pnl != null ? tone(p.unrealized_pnl) : ""}>
                      {p.unrealized_pnl != null ? money(p.unrealized_pnl) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card p-5">
          <div className="mb-3 font-medium">My Strategy</div>
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
              No strategy submitted yet — submit one from Claude.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
