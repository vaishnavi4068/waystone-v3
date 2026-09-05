"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import QueryGate from "@/components/query-gate";
import { getStrategies } from "@/lib/api";
import type { ResearchStrategy } from "@/lib/types";

const BOOKS: { id: string; label: string }[] = [
  { id: "equities", label: "Stocks" },
  { id: "options", label: "Options" },
  { id: "futures", label: "Futures" },
];

function bookClass(book: string) {
  if (book === "equities") return "bg-sky-600/20 text-sky-200";
  if (book === "options") return "bg-violet-600/20 text-violet-200";
  if (book === "futures") return "bg-amber-600/20 text-amber-200";
  return "bg-slate-800 text-slate-300";
}

function bookLabel(book: string) {
  return BOOKS.find((b) => b.id === book)?.label ?? book;
}

function Card({ row }: { row: ResearchStrategy }) {
  const stats = row.latest?.stats;
  return (
    <Link href={`/strategies/${row.id}`} className="card block p-5 hover:border-slate-600">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="font-medium">{row.name}</div>
        <span className={`rounded px-2 py-0.5 text-xs ${bookClass(row.book)}`}>{bookLabel(row.book)}</span>
      </div>
      <p className="mb-4 text-sm text-slate-400">{row.summary}</p>
      <div className="mb-3 text-xs text-slate-500">
        {row.instruments} · {row.holding_period}
      </div>
      {row.latest ? (
        <div className="grid grid-cols-3 gap-2 text-sm">
          <div>
            <div className="text-xs text-slate-500">Sharpe</div>
            <div>{stats?.sharpe ?? "—"}</div>
          </div>
          <div>
            <div className="text-xs text-slate-500">CAGR</div>
            <div>{stats?.cagr_pct != null ? `${stats.cagr_pct}%` : "—"}</div>
          </div>
          <div>
            <div className="text-xs text-slate-500">Max DD</div>
            <div>{stats?.max_drawdown_pct != null ? `${stats.max_drawdown_pct}%` : "—"}</div>
          </div>
        </div>
      ) : (
        <div className="text-xs text-slate-500">No published run yet</div>
      )}
      {row.latest?.date && (
        <div className="mt-3 text-xs text-slate-500">
          As of {row.latest.date}
          {row.latest.synthetic ? " · preview" : ""}
        </div>
      )}
    </Link>
  );
}

export default function Page() {
  const q = useQuery({ queryKey: ["strategies"], queryFn: getStrategies });
  const rows = q.data?.strategies ?? [];
  return (
    <div>
      <h1 className="mb-2 text-2xl font-semibold">Strategies</h1>
      <p className="mb-6 text-sm text-slate-400">
        Research sleeves. Mac Studio publishes dated runs to GCS; this page only reads them.
      </p>
      <QueryGate query={q}>
        {BOOKS.map((book) => {
          const group = rows.filter((r) => r.book === book.id);
          if (!group.length) return null;
          return (
            <section key={book.id} className="mb-8">
              <h2 className="mb-3 text-sm uppercase tracking-wide text-slate-500">{book.label}</h2>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                {group.map((row) => (
                  <Card key={row.id} row={row} />
                ))}
              </div>
            </section>
          );
        })}
      </QueryGate>
    </div>
  );
}
