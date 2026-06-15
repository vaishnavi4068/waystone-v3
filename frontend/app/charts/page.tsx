"use client";

import { useQuery } from "@tanstack/react-query";
import { CandlestickSeries, createChart, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import { getBars } from "@/lib/api";

export default function Page() {
  const [symbol, setSymbol] = useState("AAPL");
  const [input, setInput] = useState("AAPL");
  const { data } = useQuery({ queryKey: ["bars", symbol], queryFn: () => getBars(symbol) });
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || !data) return;
    const chart = createChart(el, {
      width: el.clientWidth,
      height: 460,
      layout: { background: { color: "transparent" }, textColor: "#94a3b8" },
      grid: { vertLines: { color: "#1e2533" }, horzLines: { color: "#1e2533" } },
      timeScale: { borderColor: "#1e2533" },
      rightPriceScale: { borderColor: "#1e2533" },
    });
    const series = chart.addSeries(CandlestickSeries);
    series.setData(
      data.map((b) => ({
        time: b.time as UTCTimestamp,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      })),
    );
    chart.timeScale().fitContent();
    const onResize = () => chart.applyOptions({ width: el.clientWidth });
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.remove();
    };
  }, [data]);

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold">Charts</h1>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setSymbol(input.trim().toUpperCase());
        }}
        className="mb-4 flex gap-2"
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 outline-none focus:border-emerald-500"
          placeholder="Symbol"
        />
        <button className="rounded-lg bg-emerald-600 px-4 py-2 hover:bg-emerald-500">
          Load
        </button>
      </form>
      <div className="card p-4">
        <div className="mb-2 text-sm text-slate-400">{symbol}</div>
        <div ref={ref} />
      </div>
    </div>
  );
}
