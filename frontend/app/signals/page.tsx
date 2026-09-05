"use client";

import { useQuery } from "@tanstack/react-query";

import QueryGate from "@/components/query-gate";
import { getSignals } from "@/lib/api";
import { tone } from "@/lib/format";

function ScoreBar({ score }: { score: number }) {
  // score in [-10, 10] -> a centered bar
  const width = Math.min(Math.abs(score) / 10, 1) * 50;
  return (
    <div className="relative h-2 w-40 rounded bg-slate-800">
      <div className="absolute left-1/2 top-0 h-2 w-px bg-slate-600" />
      <div
        className={`absolute top-0 h-2 rounded ${score >= 0 ? "bg-emerald-500" : "bg-rose-500"}`}
        style={{
          left: score >= 0 ? "50%" : `${50 - width}%`,
          width: `${width}%`,
        }}
      />
    </div>
  );
}

export default function Page() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["signals"],
    queryFn: () => getSignals(),
  });

  if (isLoading) return <QueryGate isLoading isError={false} />;
  if (isError || !data) {
    return <QueryGate isLoading={false} isError error={error} />;
  }

  return (
    <div>
      <h1 className="mb-1 text-2xl font-semibold">Signals</h1>
      <p className="mb-6 text-sm text-slate-500">
        Composite momentum score (−10…+10) and the contributor breakdown across the
        watchlist.
      </p>
      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-900/60 text-left text-slate-500">
            <tr>
              <th className="px-5 py-3">Symbol</th>
              <th>Composite</th>
              <th></th>
              <th>Contributors</th>
            </tr>
          </thead>
          <tbody>
            {data.map((s) => (
              <tr key={s.symbol} className="border-t border-slate-800 align-top">
                <td className="px-5 py-3 font-medium">{s.symbol}</td>
                <td className={`font-semibold ${tone(s.score)}`}>{s.score.toFixed(2)}</td>
                <td className="py-3">
                  <ScoreBar score={s.score} />
                </td>
                <td className="py-3">
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(s.per_contributor).map(([k, v]) => (
                      <span key={k} className="rounded bg-slate-800 px-2 py-0.5 text-xs">
                        {k} <span className={tone(v)}>{v.toFixed(1)}</span>
                      </span>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
