"use client";

import { useQuery } from "@tanstack/react-query";

import { getNews } from "@/lib/api";

export default function Page() {
  const { data, isLoading } = useQuery({ queryKey: ["news"], queryFn: () => getNews() });

  if (isLoading || !data) return <div className="text-slate-400">Loading…</div>;

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold">News</h1>
      {data.length === 0 ? (
        <div className="card p-5 text-sm text-slate-500">
          No headlines (a Polygon key must be configured on the server for news).
        </div>
      ) : (
        <div className="space-y-3">
          {data.map((n, i) => (
            <a
              key={i}
              href={n.url}
              target="_blank"
              rel="noreferrer"
              className="card block p-4 hover:border-slate-600"
            >
              <div className="font-medium">{n.title}</div>
              <div className="mt-1 text-xs text-slate-500">
                {n.source} · {new Date(n.published_at).toLocaleString()} ·{" "}
                {n.symbols.join(", ")}
              </div>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
