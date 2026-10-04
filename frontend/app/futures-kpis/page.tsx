"use client";

import { Suspense } from "react";

import { Chip } from "@/components/hq";
import { HqFrame } from "@/components/hq-filters";
import { History, KpiPanel, StrategyCards } from "@/components/hq-views";
import QueryGate from "@/components/query-gate";
import type { HqStrategy } from "@/lib/types";

function StrategyHeader({ s }: { s: HqStrategy }) {
  return (
    <div className="mb-4 flex flex-wrap items-start justify-between gap-4">
      <div>
        <div className="text-xl font-semibold">{s.display_name}</div>
        <div className="text-xs text-slate-500">
          {s.strategy_code} · {s.instrument_root}
          {s.paper_start_date ? ` · paper since ${s.paper_start_date}` : ""}
        </div>
      </div>
      <div className="max-w-md">
        <Chip wrap value={s.overall_gate} />
      </div>
    </div>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<QueryGate isLoading isError={false} />}>
      <HqFrame
        title="Futures KPIs"
        subtitle="Workbook KPI scorecard (week, month-to-date, inception-to-date) as of the selected session, computed from the HQ database."
      >
        {({ selected, futures, strategies, date, setStrategy, setDate }) =>
          selected ? (
            <>
              <StrategyHeader s={selected} />
              <KpiPanel code={selected.strategy_code} date={date} />
              <History code={selected.strategy_code} date={date} onPickDate={setDate} />
            </>
          ) : (
            <>
              <StrategyCards strategies={strategies} onPick={setStrategy} />
              {futures.map((s) => (
                <div key={s.strategy_code} className="mt-8">
                  <StrategyHeader s={s} />
                  <KpiPanel code={s.strategy_code} date={date} />
                </div>
              ))}
            </>
          )
        }
      </HqFrame>
    </Suspense>
  );
}
