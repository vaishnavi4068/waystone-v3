"use client";

import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";

function Redirect() {
  const { code } = useParams<{ code: string }>();
  const date = useSearchParams().get("date");
  const router = useRouter();
  useEffect(() => {
    router.replace(`/daily?strategy=${code}${date ? `&date=${date}` : ""}`);
  }, [code, date, router]);
  return null;
}

export default function Page() {
  return (
    <Suspense fallback={null}>
      <Redirect />
    </Suspense>
  );
}
