"use client";

import { useQuery } from "@tanstack/react-query";

import { getStandings } from "@/lib/api";
import { money, pct, tone } from "@/lib/format";

export default function Page() {
  const { data, isLoading } = useQuery({
    queryKey: ["standings"],
    queryFn: getStandings,
  });

  if (isLoading || !data) return <div className="text-slate-400">Loading…</div>;

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold">Leaderboard</h1>
      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-900/60 text-left text-slate-500">
            <tr>
              <th className="px-5 py-3">#</th>
              <th>Player</th>
              <th>Equity</th>
              <th>Return</th>
              <th>Cycles</th>
            </tr>
          </thead>
          <tbody>
            {data.map((s) => (
              <tr key={s.player} className="border-t border-slate-800">
                <td className="px-5 py-3 font-semibold">{s.rank}</td>
                <td>{s.player}</td>
                <td>{money(s.equity)}</td>
                <td className={tone(s.return_pct)}>{pct(s.return_pct)}</td>
                <td>{s.cycles_run}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
