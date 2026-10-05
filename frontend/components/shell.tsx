"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  BarChart3,
  BookOpen,
  CalendarDays,
  CandlestickChart,
  Gauge,
  GitCompare,
  LineChart,
  ListOrdered,
  LogOut,
  Newspaper,
  Wallet,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { apiErrorMessage, clearToken, getAccount } from "@/lib/api";
import { HQ_PAGES, onSelectionChange, savedSelection } from "@/lib/hq-selection";

// Hidden pages stay routable (and their APIs wired); flip `hidden` to bring them back.
const NAV = [
  { href: "/paper", label: "Paper trades", icon: ListOrdered },
  { href: "/daily", label: "Daily", icon: CalendarDays },
  { href: "/futures-kpis", label: "Futures KPIs", icon: Gauge },
  { href: "/strategies", label: "Strategies", icon: BookOpen },
  { href: "/compare", label: "Compare", icon: GitCompare },
  { href: "/options-kpis", label: "Options KPIs", icon: Gauge },
  { href: "/account", label: "Account", icon: Wallet, hidden: true },
  { href: "/positions", label: "Positions", icon: BarChart3, hidden: true },
  { href: "/orders", label: "Orders", icon: ListOrdered, hidden: true },
  { href: "/activity", label: "Activity", icon: Activity, hidden: true },
  { href: "/ibkr", label: "IBKR report", icon: CalendarDays },
  { href: "/signals", label: "Signals", icon: LineChart, hidden: true },
  { href: "/charts", label: "Charts", icon: CandlestickChart, hidden: true },
  { href: "/backtests", label: "Backtests", icon: BarChart3, hidden: true },
  { href: "/news", label: "News", icon: Newspaper, hidden: true },
];

export default function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [selection, setSelection] = useState("");
  useEffect(() => {
    setSelection(savedSelection());
    return onSelectionChange(() => setSelection(savedSelection()));
  }, []);
  const acct = useQuery({ queryKey: ["account"], queryFn: getAccount });

  function logout() {
    clearToken();
    window.location.reload();
  }

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 flex-col border-r border-slate-800 p-4">
        <div className="mb-6">
          <div className="text-lg font-semibold">HQCapital</div>
          <div className="text-xs text-slate-500">IBKR live</div>
        </div>
        <nav className="flex flex-1 flex-col gap-1">
          {NAV.filter((n) => !n.hidden).map(({ href, label, icon: Icon }) => {
            const active =
              href === "/strategies" ? pathname.startsWith(href) : pathname === href;
            return (
              <Link
                key={href}
                href={HQ_PAGES.includes(href) && selection ? `${href}?${selection}` : href}
                className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${
                  active
                    ? "bg-emerald-600/20 text-emerald-300"
                    : "text-slate-300 hover:bg-slate-800"
                }`}
              >
                <Icon size={18} />
                {label}
              </Link>
            );
          })}
        </nav>
        <div className="mt-4 border-t border-slate-800 pt-4">
          <div className="text-sm font-medium">{acct.data?.you ?? (acct.isError ? "API down" : "…")}</div>
          {acct.data && (
            <div className="text-xs text-slate-500">
              {acct.data.broker} · {acct.data.is_paper ? "paper" : "LIVE"}
              {acct.data.trading_enabled ? "" : " · halted"}
            </div>
          )}
          {acct.isError && (
            <div className="mt-1 text-xs text-rose-300">{apiErrorMessage(acct.error)}</div>
          )}
          <button
            onClick={logout}
            className="mt-3 flex items-center gap-2 text-xs text-slate-400 hover:text-slate-200"
          >
            <LogOut size={14} /> Sign out
          </button>
        </div>
      </aside>
      <main className="flex-1 overflow-auto p-8">{children}</main>
    </div>
  );
}
