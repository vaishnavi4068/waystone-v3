"use client";

import { Suspense } from "react";

import { AllStrategiesReport, StrategyDay } from "@/components/hq-daily";
import { HqFrame } from "@/components/hq-filters";
import QueryGate from "@/components/query-gate";

export default function Page() {
  return (
    <Suspense fallback={<QueryGate isLoading isError={false} />}>
      <HqFrame
        title="Daily"
        subtitle="What happened each session, live paper vs the backtest replay, in plain language. Paper logs load every hour at :35; backtest replays at 16:35 and 17:35 ET."
      >
        {({ selected, futures, date, setStrategy }) =>
          selected ? (
            <StrategyDay strategy={selected} date={date} />
          ) : (
            <AllStrategiesReport date={date} strategies={futures} onPick={setStrategy} />
          )
        }
      </HqFrame>
    </Suspense>
  );
}
