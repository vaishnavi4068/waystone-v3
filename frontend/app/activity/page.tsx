"use client";

import { useQuery } from "@tanstack/react-query";

import QueryGate from "@/components/query-gate";
import { getActivity } from "@/lib/api";

export default function Page() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["activity"],
    queryFn: getActivity,
  });
  if (isLoading) return <QueryGate isLoading isError={false} />;
  if (isError || !data) {
    return <QueryGate isLoading={false} isError error={error} />;
  }

  return (
    <div>
      <h1 className="mb-1 text-2xl font-semibold">Activity</h1>
      <p className="mb-6 text-sm text-slate-500">
        Who did what on the shared account — strategy changes, cycles, halts.
      </p>
      {data.length === 0 ? (
        <div className="card p-5 text-sm text-slate-500">No activity yet.</div>
      ) : (
        <div className="card divide-y divide-slate-800">
          {data.map((e) => (
            <div key={e.seq} className="flex items-center gap-3 px-5 py-3 text-sm">
              <span className="rounded bg-slate-800 px-2 py-0.5 text-xs">{e.action}</span>
              <span className="font-medium">{e.actor}</span>
              <span className="text-slate-400">{e.detail}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
