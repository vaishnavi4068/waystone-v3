"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiErrorMessage, getResearchOps, postResearchInbox } from "@/lib/api";

const ACTIONS = [
  { id: "approve-fetch", label: "Approve fetch" },
  { id: "approve-run", label: "Approve run" },
  { id: "approve-publish", label: "Approve publish" },
] as const;

export default function ResearchOpsPanel() {
  const queryClient = useQueryClient();
  const ops = useQuery({ queryKey: ["research-ops"], queryFn: getResearchOps });
  const approve = useMutation({
    mutationFn: (action: string) =>
      postResearchInbox(`HQ approved ${action.replace("approve-", "")}`, action),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["research-ops"] });
    },
  });

  if (ops.isLoading) {
    return <div className="card mb-6 p-4 text-sm text-slate-500">Loading Mac / Grok Bot ops…</div>;
  }
  if (ops.isError) {
    return (
      <div className="card mb-6 p-4 text-sm text-slate-500">
        Ops channel unavailable: {apiErrorMessage(ops.error)}
      </div>
    );
  }

  const status = ops.data?.status;
  const inbox = ops.data?.inbox ?? [];
  const writable = ops.data?.writable ?? false;

  return (
    <section className="card mb-6 p-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-500">Mac Studio · Grok Bot</div>
          <div className="mt-1 text-sm text-slate-300">
            {status?.title || "No status posted yet"}
          </div>
          {status?.body && <div className="mt-1 text-xs text-slate-500">{status.body}</div>}
          {status?.at && (
            <div className="mt-1 text-xs text-slate-600">
              {status.phase}
              {status.approval ? ` · waiting on ${status.approval}` : ""} · {status.at}
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {ACTIONS.map((action) => (
            <button
              key={action.id}
              type="button"
              disabled={!writable || approve.isPending}
              onClick={() => approve.mutate(action.id)}
              className="rounded-lg bg-emerald-600/20 px-3 py-1.5 text-sm text-emerald-300 hover:bg-emerald-600/30 disabled:opacity-50"
            >
              {action.label}
            </button>
          ))}
        </div>
      </div>
      {!writable && (
        <p className="text-xs text-slate-500">
          Approvals need a report store (`IBKR_REPORTS_BUCKET` or `IBKR_REPORTS_LOCAL_DIR`). Until
          then this page only reads staged strategy fixtures.
        </p>
      )}
      {approve.isError && (
        <p className="mt-2 text-xs text-rose-400">{apiErrorMessage(approve.error)}</p>
      )}
      {inbox.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs text-slate-400">
          {inbox.map((row) => (
            <li key={row.id}>
              pending {row.action || "note"} · {row.text} · {row.source}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
