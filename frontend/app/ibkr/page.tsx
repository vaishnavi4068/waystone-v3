"use client";

import { useQuery } from "@tanstack/react-query";
import { CalendarDays } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import QueryGate from "@/components/query-gate";
import StagedBanner from "@/components/staged-banner";
import { getIbkrDays, getIbkrReport, isNotFound } from "@/lib/api";
import { contractLabel, money, tone } from "@/lib/format";
import type { IbkrExecution } from "@/lib/types";

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

function BookCard({
  title,
  fills,
  notional,
  commission,
  pnl,
}: {
  title: string;
  fills: number;
  notional: number;
  commission: number;
  pnl: number;
}) {
  return (
    <div className="card p-5">
      <div className="mb-3 font-medium">{title}</div>
      <div className="grid grid-cols-2 gap-3 text-sm">
        <div>
          <div className="text-slate-500">Fills</div>
          <div>{fills}</div>
        </div>
        <div>
          <div className="text-slate-500">Notional</div>
          <div>{money(notional)}</div>
        </div>
        <div>
          <div className="text-slate-500">Commission</div>
          <div>{money(commission)}</div>
        </div>
        <div>
          <div className="text-slate-500">Realized P&L</div>
          <div className={tone(pnl)}>{money(pnl)}</div>
        </div>
      </div>
    </div>
  );
}

function sideClass(side: string) {
  const token = side.toUpperCase();
  return token === "BOT" || token === "BUY" ? "text-emerald-400" : "text-rose-400";
}

export default function Page() {
  const days = useQuery({ queryKey: ["ibkr-days"], queryFn: getIbkrDays });
  const [date, setDate] = useState("");
  const [book, setBook] = useState<"all" | "futures" | "options">("all");

  useEffect(() => {
    if (days.data?.latest && !date) setDate(days.data.latest);
  }, [days.data, date]);

  const report = useQuery({
    queryKey: ["ibkr-report", date],
    queryFn: () => getIbkrReport(date),
    enabled: Boolean(date),
  });

  const fills = useMemo(() => {
    const rows: IbkrExecution[] = report.data?.executions ?? [];
    if (book === "all") return rows;
    return rows.filter((r) => r.book === book);
  }, [report.data, book]);

  if (days.isLoading) {
    return <QueryGate isLoading isError={false} />;
  }
  if (days.isError && !isNotFound(days.error)) {
    return <QueryGate isLoading={false} isError error={days.error} />;
  }
  if (days.isError || !days.data) {
    return (
      <div className="card p-5 text-sm text-slate-400">
        IBKR daily reports are not configured, or no dump has been published yet. Run{" "}
        <code className="text-slate-200">waystone3 ibkr-export</code> on the VM.
      </div>
    );
  }

  const unpublished = !days.data.today_published;

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Daily</h1>
          <p className="mt-1 text-sm text-slate-500">
            IBKR fills and EOD snapshot. Published when you run the dump CLI.
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm text-slate-400">
          <CalendarDays size={16} />
          <select
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-200"
          >
            {days.data.days.length === 0 && <option value="">No published days</option>}
            {days.data.days
              .slice()
              .reverse()
              .map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
          </select>
        </label>
      </div>

      {(days.data.staged || report.data?.staged) && (
        <StagedBanner week={report.data?.staged_week ?? days.data.staged_week} />
      )}

      {unpublished && (
        <div className="mb-6 rounded-lg border border-amber-700/40 bg-amber-950/30 px-4 py-3 text-sm text-amber-200">
          Today ({days.data.today}) is not published yet. Showing the last dump
          {days.data.latest ? ` (${days.data.latest})` : ""}. Run{" "}
          <code className="text-amber-100">waystone3 ibkr-export</code> after the session.
        </div>
      )}

      {report.isLoading && <div className="text-slate-400">Loading report…</div>}
      {report.isError && (
        <QueryGate isLoading={false} isError error={report.error} />
      )}
      {report.data && (
        <>
          <p className="mb-4 text-xs text-slate-500">
            As of {new Date(report.data.generated_at).toLocaleString()} · {report.data.date}
          </p>
          <div className="mb-6 grid grid-cols-1 gap-4 md:grid-cols-4">
            <Stat label="NLV" value={money(report.data.account.nlv)} />
            <Stat label="Cash" value={money(report.data.account.cash)} />
            <Stat
              label="Day realized P&L"
              value={money(report.data.summary.totals.realized_pnl)}
              className={tone(report.data.summary.totals.realized_pnl)}
            />
            <Stat label="Commissions" value={money(report.data.summary.totals.commission)} />
          </div>
          <div className="mb-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <BookCard
              title="Futures"
              fills={report.data.summary.futures.fills}
              notional={report.data.summary.futures.notional}
              commission={report.data.summary.futures.commission}
              pnl={report.data.summary.futures.realized_pnl}
            />
            <BookCard
              title="Options"
              fills={report.data.summary.options.fills}
              notional={report.data.summary.options.notional}
              commission={report.data.summary.options.commission}
              pnl={report.data.summary.options.realized_pnl}
            />
          </div>
          <div className="mb-3 flex gap-2 text-sm">
            {(["all", "futures", "options"] as const).map((key) => (
              <button
                key={key}
                type="button"
                onClick={() => setBook(key)}
                className={`rounded-lg px-3 py-1.5 ${
                  book === key ? "bg-emerald-600/20 text-emerald-300" : "bg-slate-800 text-slate-300"
                }`}
              >
                {key === "all" ? "All fills" : key === "futures" ? "Futures" : "Options"}
              </button>
            ))}
          </div>
          {fills.length === 0 ? (
            <div className="card p-5 text-sm text-slate-500">No fills for this filter.</div>
          ) : (
            <div className="card overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-slate-900/60 text-left text-slate-500">
                  <tr>
                    <th className="px-5 py-3">Contract</th>
                    <th>Book</th>
                    <th>Side</th>
                    <th>Qty</th>
                    <th>Price</th>
                    <th>Commission</th>
                    <th>Realized</th>
                    <th>Time</th>
                  </tr>
                </thead>
                <tbody>
                  {fills.map((f) => (
                    <tr key={f.exec_id} className="border-t border-slate-800">
                      <td className="px-5 py-3 font-medium">
                        {contractLabel(f)}
                        <div className="text-xs font-normal text-slate-500">
                          {[f.expiry, f.strike, f.right].filter(Boolean).join(" ")}
                        </div>
                      </td>
                      <td className="capitalize">{f.book}</td>
                      <td className={sideClass(f.side)}>{f.side}</td>
                      <td>{f.qty}</td>
                      <td>{money(f.price)}</td>
                      <td>{f.commission != null ? money(f.commission) : "—"}</td>
                      <td className={f.realized_pnl != null ? tone(f.realized_pnl) : ""}>
                        {f.realized_pnl != null ? money(f.realized_pnl) : "—"}
                      </td>
                      <td className="text-slate-500">{new Date(f.time).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
