"use client";

import { apiErrorMessage } from "@/lib/api";

export default function QueryGate({
  isLoading,
  isError,
  error,
  children,
}: {
  isLoading: boolean;
  isError: boolean;
  error?: unknown;
  children: React.ReactNode;
}) {
  if (isLoading) return <div className="text-slate-400">Loading…</div>;
  if (isError) {
    return (
      <div className="card p-5 text-sm text-slate-300">
        <div className="font-medium text-rose-300">Dashboard API did not respond</div>
        <p className="mt-2 text-slate-400">{apiErrorMessage(error)}</p>
      </div>
    );
  }
  return <>{children}</>;
}
