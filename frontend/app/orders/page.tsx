"use client";

import { useQuery } from "@tanstack/react-query";

import { getOrders } from "@/lib/api";
import { money } from "@/lib/format";

export default function Page() {
  const { data, isLoading } = useQuery({ queryKey: ["orders"], queryFn: getOrders });
  if (isLoading || !data) return <div className="text-slate-400">Loading…</div>;

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold">Orders</h1>
      {data.length === 0 ? (
        <div className="card p-5 text-sm text-slate-500">No orders yet.</div>
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/60 text-left text-slate-500">
              <tr>
                <th className="px-5 py-3">Symbol</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Status</th>
                <th>Fill</th>
                <th>Submitted</th>
              </tr>
            </thead>
            <tbody>
              {data.map((o, i) => (
                <tr key={i} className="border-t border-slate-800">
                  <td className="px-5 py-3 font-medium">{o.symbol}</td>
                  <td className={o.side === "buy" ? "text-emerald-400" : "text-rose-400"}>
                    {o.side.toUpperCase()}
                  </td>
                  <td>{o.qty}</td>
                  <td>{o.status}</td>
                  <td>{o.avg_fill_price != null ? money(o.avg_fill_price) : "—"}</td>
                  <td className="text-slate-500">
                    {new Date(o.submitted_at).toLocaleString()}
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
