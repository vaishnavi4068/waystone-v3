"use client";

import { useQuery } from "@tanstack/react-query";

import QueryGate from "@/components/query-gate";
import { getOrders } from "@/lib/api";
import { contractLabel, money, tone } from "@/lib/format";

export default function Page() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["orders"],
    queryFn: getOrders,
  });
  if (isLoading) return <QueryGate isLoading isError={false} />;
  if (isError || !data) {
    return <QueryGate isLoading={false} isError error={error} />;
  }

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold">Orders</h1>
      {data.length === 0 ? (
        <div className="card p-5 text-sm text-slate-500">No orders yet.</div>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/60 text-left text-slate-500">
              <tr>
                <th className="px-5 py-3">Contract</th>
                <th>Book</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Status</th>
                <th>Fill</th>
                <th>Commission</th>
                <th>Realized</th>
                <th>Submitted</th>
              </tr>
            </thead>
            <tbody>
              {data.map((o, i) => (
                <tr key={i} className="border-t border-slate-800">
                  <td className="px-5 py-3 font-medium">
                    {contractLabel(o)}
                    <div className="text-xs font-normal text-slate-500">
                      {[o.expiry, o.strike, o.right].filter(Boolean).join(" ")}
                    </div>
                  </td>
                  <td className="capitalize">{o.book ?? "—"}</td>
                  <td className={o.side === "buy" ? "text-emerald-400" : "text-rose-400"}>
                    {o.side.toUpperCase()}
                  </td>
                  <td>{o.qty}</td>
                  <td>{o.status}</td>
                  <td>{o.avg_fill_price != null ? money(o.avg_fill_price) : "—"}</td>
                  <td>{o.commission != null ? money(o.commission) : "—"}</td>
                  <td className={o.realized_pnl != null ? tone(o.realized_pnl) : ""}>
                    {o.realized_pnl != null ? money(o.realized_pnl) : "—"}
                  </td>
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
