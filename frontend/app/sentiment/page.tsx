"use client";

import { Suspense } from "react";

import QueryGate from "@/components/query-gate";
import SentimentDashboard from "@/components/sentiment";

export default function Page() {
  return (
    <Suspense fallback={<QueryGate isLoading isError={false} />}>
      <SentimentDashboard />
    </Suspense>
  );
}
