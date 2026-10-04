"use client";

import type { HqKpi } from "@/lib/types";

export function statusClass(status?: string | null) {
  const s = (status ?? "").toUpperCase();
  if (s === "GREEN" || s === "OK" || s === "FINAL" || s === "LOADED" || s.startsWith("ALL GREEN")) {
    return "bg-emerald-600/20 text-emerald-300";
  }
  if (s === "AMBER" || s.startsWith("PASSING") || s === "PRELIMINARY" || s === "INTRADAY" || s === "PENDING") {
    return "bg-amber-600/20 text-amber-200";
  }
  if (s === "RED" || s === "FLAG" || s.startsWith("RED") || s === "MISSING" || s === "DATA_INCOMPLETE" || s === "FAILED") {
    return "bg-rose-600/20 text-rose-300";
  }
  if (s.startsWith("INSUFFICIENT")) return "bg-sky-600/20 text-sky-200";
  return "bg-slate-800 text-slate-400";
}

export function Chip({ value, title, wrap }: { value?: string | null; title?: string; wrap?: boolean }) {
  if (!value) return <span className="text-slate-600">—</span>;
  const layout = wrap ? "inline-block leading-snug" : "whitespace-nowrap";
  return (
    <span title={title} className={`${layout} rounded px-2 py-0.5 text-xs ${statusClass(value)}`}>
      {value}
    </span>
  );
}

export const usd = (n?: number | null) =>
  n == null
    ? "—"
    : `${n < 0 ? "-" : ""}$${Math.abs(n).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;

export const frac = (n?: number | null, digits = 2) =>
  n == null ? "—" : `${n >= 0 ? "" : "-"}${Math.abs(n * 100).toFixed(digits)}%`;

export const num = (n?: number | null, digits = 2) => (n == null ? "—" : n.toFixed(digits));

export const signTone = (n?: number | null) =>
  n == null ? "text-slate-500" : n > 0 ? "text-emerald-400" : n < 0 ? "text-rose-400" : "text-slate-300";

export const time = (ts?: string | null) =>
  ts
    ? new Date(ts).toLocaleTimeString("en-US", {
        timeZone: "America/New_York",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "—";

export const stamp = (ts?: string | null) =>
  ts
    ? new Date(ts).toLocaleString("en-US", {
        timeZone: "America/New_York",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }) + " ET"
    : "—";

function byUnit(value: number, unit: string | null) {
  if (unit === "pct") return frac(value);
  if (unit === "usd") return usd(value);
  if (unit === "count") return String(Math.round(value));
  if (unit === "months") return value.toFixed(1);
  return value.toFixed(2);
}

export function kpiValue(k: HqKpi) {
  if (k.num_value != null) return byUnit(k.num_value, k.unit);
  return k.text_value ?? "—";
}

export function kpiTarget(k: HqKpi, at: number | null) {
  if (at == null) return "—";
  return `${k.direction === "Lower" ? "≤" : "≥"} ${byUnit(at, k.unit)}`;
}
