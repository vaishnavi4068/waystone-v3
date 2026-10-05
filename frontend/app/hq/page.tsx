"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";

import QueryGate from "@/components/query-gate";

function Redirect() {
  const router = useRouter();
  const params = useSearchParams();
  useEffect(() => {
    const q = params.toString();
    router.replace(`/daily${q ? `?${q}` : ""}`);
  }, [params, router]);
  return <QueryGate isLoading isError={false} />;
}

export default function Page() {
  return (
    <Suspense fallback={<QueryGate isLoading isError={false} />}>
      <Redirect />
    </Suspense>
  );
}
