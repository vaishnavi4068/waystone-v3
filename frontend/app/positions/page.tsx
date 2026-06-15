"use client";

import { useQuery } from "@tanstack/react-query";

import { getPositions } from "@/lib/api";
import { money, tone } from "@/lib/format";

export default function Page() {
  const { data, isLoading } = useQuery({ queryKey: ["positions"], queryFn: getPositions });
  if (isLoading || !data) return <div className="text-slate-400">Loading…</div>;

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold">Positions</h1>
      {data.length === 0 ? (
        <div className="card p-5 text-sm text-slate-500">No open positions.</div>
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/60 text-left text-slate-500">
              <tr>
                <th className="px-5 py-3">Symbol</th>
                <th>Qty</th>
                <th>Entry</th>
                <th>Mark</th>
                <th>Unrealized</th>
              </tr>
            </thead>
            <tbody>
              {data.map((p) => (
                <tr key={p.symbol} className="border-t border-slate-800">
                  <td className="px-5 py-3 font-medium">{p.symbol}</td>
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
        </div>
      )}
    </div>
  );
}
