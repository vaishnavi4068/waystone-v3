export const money = (n: number) =>
  `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

export const pct = (n: number) =>
  `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;

export const tone = (n: number) =>
  n > 0 ? "text-emerald-400" : n < 0 ? "text-rose-400" : "text-slate-300";
