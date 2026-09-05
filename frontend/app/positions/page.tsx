"use client";

import { useQuery } from "@tanstack/react-query";

import QueryGate from "@/components/query-gate";
import StagedBanner from "@/components/staged-banner";
import { getAccount, getPositions } from "@/lib/api";
import { contractLabel, money, tone } from "@/lib/format";

export default function Page() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["positions"],
    queryFn: getPositions,
  });
  const account = useQuery({ queryKey: ["account"], queryFn: getAccount });
  if (isLoading) return <QueryGate isLoading isError={false} />;
  if (isError || !data) {
    return <QueryGate isLoading={false} isError error={error} />;
  }

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold">Positions</h1>
      {account.data?.staged ? <StagedBanner week={account.data.staged_week} /> : null}
      {data.length === 0 ? (
        <div className="card p-5 text-sm text-slate-500">No open positions.</div>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/60 text-left text-slate-500">
              <tr>
                <th className="px-5 py-3">Contract</th>
                <th>Book</th>
                <th>Qty</th>
                <th>Entry</th>
                <th>Mark</th>
                <th>Unrealized</th>
              </tr>
            </thead>
            <tbody>
              {data.map((p) => (
                <tr key={`${p.local_symbol ?? p.symbol}-${p.expiry ?? ""}`} className="border-t border-slate-800">
                  <td className="px-5 py-3 font-medium">
                    {contractLabel(p)}
                    <div className="text-xs font-normal text-slate-500">
                      {[p.expiry, p.strike, p.right].filter(Boolean).join(" ")}
                    </div>
                  </td>
                  <td className="capitalize">{p.book ?? "—"}</td>
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
