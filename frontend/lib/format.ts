export const money = (n: number) =>
  `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

export const pct = (n: number) =>
  `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;

export const tone = (n: number) =>
  n > 0 ? "text-emerald-400" : n < 0 ? "text-rose-400" : "text-slate-300";

export const contractLabel = (row: {
  local_symbol?: string | null;
  symbol: string;
  expiry?: string | null;
  strike?: number | null;
  right?: string | null;
}) => {
  if (row.local_symbol) return row.local_symbol;
  const bits = [row.symbol];
  if (row.expiry) bits.push(row.expiry);
  if (row.strike != null) bits.push(String(row.strike));
  if (row.right) bits.push(row.right);
  return bits.join(" ");
};

