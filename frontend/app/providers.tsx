"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import Login from "@/components/login";
import Shell from "@/components/shell";
import { getToken } from "@/lib/api";

function makeClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: 1,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        // IBKR dumps are EOD; 15s polling stacked hung requests and looked like a freeze.
        refetchInterval: false,
      },
    },
  });
}

export default function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(makeClient);
  const [ready, setReady] = useState(false);
  const [authed, setAuthed] = useState(false);

  useEffect(() => {
    setAuthed(!!getToken());
    setReady(true);
  }, []);

  if (!ready) return <div className="p-8 text-slate-400">Loading…</div>;

  return (
    <QueryClientProvider client={queryClient}>
      {authed ? (
        <Shell>{children}</Shell>
      ) : (
        <Login onLogin={() => setAuthed(true)} />
      )}
    </QueryClientProvider>
  );
}
