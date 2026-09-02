"use client";

import { useState } from "react";

import { apiErrorMessage, clearToken, getAccount, setToken } from "@/lib/api";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!value.trim() || busy) return;
    setBusy(true);
    setError("");
    setToken(value.trim());
    try {
      await getAccount();
      onLogin();
    } catch (err) {
      clearToken();
      setError(apiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <form onSubmit={submit} className="card w-full max-w-md p-8">
        <h1 className="text-2xl font-semibold">Waystone</h1>
        <p className="mt-1 text-sm text-slate-400">
          Paste your access token to view the IBKR daily dashboard.
        </p>
        <input
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Bearer token"
          className="mt-6 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 outline-none focus:border-emerald-500"
        />
        {error ? <p className="mt-3 text-sm text-rose-300">{error}</p> : null}
        <button
          type="submit"
          disabled={busy}
          className="mt-4 w-full rounded-lg bg-emerald-600 px-3 py-2 font-medium hover:bg-emerald-500 disabled:opacity-60"
        >
          {busy ? "Connecting…" : "Enter"}
        </button>
        <p className="mt-4 text-xs text-slate-500">
          Your token is stored only in this browser. Everything here is read-only.
        </p>
      </form>
    </div>
  );
}
