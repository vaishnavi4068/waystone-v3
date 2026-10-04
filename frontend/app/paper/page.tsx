"use client";

import { Suspense } from "react";

import { HqFrame } from "@/components/hq-filters";
import { PaperDay } from "@/components/hq-paper";
import QueryGate from "@/components/query-gate";

export default function Page() {
  return (
    <Suspense fallback={<QueryGate isLoading isError={false} />}>
      <HqFrame
        title="Paper trades"
        subtitle="Live paper trading activity from the HQ database: trades, signals, IB fills and engine events, with when each piece of data last arrived."
      >
        {({ selected, date }) => <PaperDay strategy={selected} date={date} />}
      </HqFrame>
    </Suspense>
  );
}
