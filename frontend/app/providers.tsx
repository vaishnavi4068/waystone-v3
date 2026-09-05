"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import Login from "@/components/login";
import Shell from "@/components/shell";
import { clearToken, getAccount, getToken } from "@/lib/api";

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchInterval: 15000, retry: 1 } },
});

export default function Providers({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [authed, setAuthed] = useState(false);

  useEffect(() => {
    const token = getToken();
    if (!token) {
      setReady(true);
      return;
    }
    getAccount()
      .then(() => setAuthed(true))
      .catch(() => clearToken())
      .finally(() => setReady(true));
  }, []);

  if (!ready) return null;

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
