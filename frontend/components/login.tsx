"use client";

import { useState } from "react";

import { apiErrorMessage, login } from "@/lib/api";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password || busy) return;
    setBusy(true);
    setError("");
    try {
      await login(username.trim(), password);
      onLogin();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <form onSubmit={submit} className="card w-full max-w-md p-8">
        <h1 className="text-2xl font-semibold">HQCapital</h1>
        <p className="mt-1 text-sm text-slate-400">
          Sign in with your team username and password to view the IBKR daily dashboard.
        </p>
        <label className="mt-6 block text-xs uppercase tracking-wide text-slate-500">
          Username
          <input
            type="text"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="Your name"
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 outline-none focus:border-emerald-500"
          />
        </label>
        <label className="mt-4 block text-xs uppercase tracking-wide text-slate-500">
          Password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Your password"
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 outline-none focus:border-emerald-500"
          />
        </label>
        {error ? <p className="mt-3 text-sm text-rose-300">{error}</p> : null}
        <button
          type="submit"
          disabled={busy || !username.trim() || !password}
          className="mt-4 w-full rounded-lg bg-emerald-600 px-3 py-2 font-medium hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="mt-4 text-xs text-slate-500">
          Your session is stored only in this browser. Everything here is read-only.
        </p>
      </form>
    </div>
  );
}
