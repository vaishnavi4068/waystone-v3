"use client";

export default function StagedBanner({
  week = "week of 10 Aug 2026",
}: {
  week?: string | null;
}) {
  return (
    <div className="mb-6 rounded-lg border border-violet-700/40 bg-violet-950/30 px-4 py-3 text-sm text-violet-100">
      <span className="mr-2 rounded bg-violet-600/30 px-2 py-0.5 text-xs font-semibold tracking-wide">
        STAGED DATA
      </span>
      Sample data for {week ?? "week of 10 Aug 2026"} (Options KPIs, Futures KPIs,
      Daily, Compare, Account). Source column is unchanged — point real feeds later.
    </div>
  );
}
