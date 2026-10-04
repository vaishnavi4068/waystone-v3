"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";

import { LoaderStatus } from "@/components/hq-views";
import QueryGate from "@/components/query-gate";
import { getHqDaily, getHqStrategies, getHqSync, isNotFound } from "@/lib/api";
import type { HqStrategy } from "@/lib/types";

export const ALL = "all";
const STORE = "hq-selection";
const DATES_REFRESH_MS = 10 * 60 * 1000;

export interface HqSelection {
  strategies: HqStrategy[];
  futures: HqStrategy[];
  selected: HqStrategy | undefined;
  date: string | null;
  setStrategy: (code: string) => void;
  setDate: (date: string) => void;
}

/** Strategy + session date, kept in the URL so Daily and Futures KPIs share links. */
export function useHqSelection() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const strategy = params.get("strategy") ?? ALL;
  const requested = params.get("date");
  const query = params.toString();

  useEffect(() => {
    if (query) sessionStorage.setItem(STORE, query);
    else {
      const saved = sessionStorage.getItem(STORE);
      if (saved) router.replace(`${pathname}?${saved}`, { scroll: false });
    }
  }, [query, pathname, router]);

  const strategies = useQuery({ queryKey: ["hq-strategies"], queryFn: getHqStrategies });
  const allDates = useQuery({
    queryKey: ["hq-sync-dates"],
    queryFn: () => getHqSync(),
    enabled: strategy === ALL,
    refetchInterval: DATES_REFRESH_MS,
  });
  const daily = useQuery({
    queryKey: ["hq-daily", strategy],
    queryFn: () => getHqDaily(strategy),
    enabled: strategy !== ALL,
    refetchInterval: DATES_REFRESH_MS,
  });

  const dates =
    strategy === ALL ? (allDates.data?.dates ?? []) : (daily.data?.sync ?? []).map((r) => r.session_date).sort();
  const date = requested && dates.includes(requested) ? requested : (dates[dates.length - 1] ?? null);

  function go(next: { strategy?: string; date?: string | null }) {
    const q = new URLSearchParams();
    const s = next.strategy ?? strategy;
    const d = next.date === undefined ? date : next.date;
    if (s !== ALL) q.set("strategy", s);
    if (d) q.set("date", d);
    sessionStorage.setItem(STORE, q.toString());
    router.replace(`${pathname}${q.toString() ? `?${q}` : ""}`, { scroll: false });
  }

  const list = strategies.data?.strategies ?? [];
  const futures = list.filter((s) => s.asset_class === "future");
  const selection: HqSelection = {
    strategies: list,
    futures,
    selected: futures.find((s) => s.strategy_code === strategy),
    date,
    setStrategy: (s) => go({ strategy: s, date: requested }),
    setDate: (d) => go({ date: d }),
  };
  const loading = strategies.isLoading || (strategy === ALL ? allDates.isLoading : daily.isLoading);
  return { selection, dates, loading, error: strategies.isError ? strategies.error : null };
}

export function HqFrame({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: (s: HqSelection & { date: string }) => React.ReactNode;
}) {
  const { selection, dates, loading, error } = useHqSelection();
  if (error && isNotFound(error)) {
    return (
      <div className="card p-5 text-sm text-slate-400">
        The HQ database is not connected. Set <code className="text-slate-200">WAYSTONE_HQ_DB_HOST</code> and the
        read-only password on the API (<code className="text-slate-200">deploy/db/bootstrap_gcp.sh dash</code>).
      </div>
    );
  }
  if (error) return <QueryGate isLoading={false} isError error={error} />;
  const { date } = selection;
  return (
    <div>
      <div className="mb-4">
        <h1 className="text-2xl font-semibold">{title}</h1>
        <p className="mt-1 text-sm text-slate-500">{subtitle}</p>
      </div>
      <FilterBar selection={selection} dates={dates} />
      {loading ? <QueryGate isLoading isError={false} /> : null}
      {!loading && !date ? (
        <div className="card p-5 text-sm text-slate-400">No sessions loaded yet for this selection.</div>
      ) : null}
      {!loading && date ? children({ ...selection, date }) : null}
    </div>
  );
}

function FilterBar({ selection, dates }: { selection: HqSelection; dates: string[] }) {
  const { selected, futures, date, setStrategy, setDate } = selection;
  const newestFirst = [...dates].reverse();
  const i = date ? newestFirst.indexOf(date) : -1;
  const older = i >= 0 ? newestFirst[i + 1] : undefined;
  const newer = i > 0 ? newestFirst[i - 1] : undefined;
  const field = "rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100";
  return (
    <div className="card sticky top-0 z-10 mb-6 flex flex-wrap items-end gap-6 px-5 py-4">
      <label className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-slate-500">Strategy</span>
        <select
          className={`${field} min-w-56`}
          value={selected?.strategy_code ?? ALL}
          onChange={(e) => setStrategy(e.target.value)}
        >
          <option value={ALL}>All strategies</option>
          {futures.map((s) => (
            <option key={s.strategy_code} value={s.strategy_code}>
              {s.display_name}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-slate-500">Date</span>
        <div className="flex items-center gap-1">
          <button
            aria-label="Previous session"
            disabled={!older}
            onClick={() => older && setDate(older)}
            className="rounded-lg border border-slate-700 p-2 text-slate-300 hover:bg-slate-800 disabled:opacity-30"
          >
            <ChevronLeft size={16} />
          </button>
          <select
            className={`${field} min-w-40`}
            value={date ?? ""}
            disabled={dates.length === 0}
            onChange={(e) => setDate(e.target.value)}
          >
            {dates.length === 0 ? <option value="">No sessions yet</option> : null}
            {newestFirst.map((d, n) => (
              <option key={d} value={d}>
                {d}
                {n === 0 ? " (latest)" : ""}
              </option>
            ))}
          </select>
          <button
            aria-label="Next session"
            disabled={!newer}
            onClick={() => newer && setDate(newer)}
            className="rounded-lg border border-slate-700 p-2 text-slate-300 hover:bg-slate-800 disabled:opacity-30"
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </label>
      <div className="ml-auto self-center">
        <LoaderStatus />
      </div>
    </div>
  );
}
