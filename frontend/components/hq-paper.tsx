"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Chip, frac, num, signTone, stamp, time, usd } from "@/components/hq";
import { Section } from "@/components/hq-views";
import QueryGate from "@/components/query-gate";
import { getHqPaper } from "@/lib/api";
import type { HqFreshness, HqPaperDay, HqStrategy } from "@/lib/types";

const REFRESH_MS = 5 * 60 * 1000;

function ago(ts?: string | null) {
  if (!ts) return "";
  const min = Math.round((Date.now() - new Date(ts).getTime()) / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min} min ago`;
  const h = Math.floor(min / 60);
  if (h < 48) return `${h}h ${min % 60}m ago`;
  return `${Math.floor(h / 24)} days ago`;
}

function nextHourlyLoad() {
  const next = new Date();
  next.setSeconds(0, 0);
  if (next.getMinutes() >= 35) next.setHours(next.getHours() + 1);
  next.setMinutes(35);
  return next.toISOString();
}

function Tile({ label, value, tone, sub }: { label: string; value: string; tone?: string; sub?: string }) {
  return (
    <div className="card px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${tone ?? ""}`}>{value}</div>
      {sub ? <div className="mt-0.5 text-xs text-slate-500">{sub}</div> : null}
    </div>
  );
}

function Freshness({ rows, load }: { rows: HqFreshness[]; load: HqPaperDay["last_paper_load"] }) {
  return (
    <Section
      title="Data freshness"
      subtitle={`The VM syncs logs to GCS; the loader reads them into Postgres every hour at :35. Next load ${stamp(nextHourlyLoad())}.`}
      right={
        load ? (
          <div className="flex items-center gap-2 text-xs text-slate-500">
            last {load.job} load <Chip value={load.status} /> {stamp(load.finished_at ?? load.started_at)} (
            {ago(load.finished_at ?? load.started_at)}) · {load.files_loaded}/{load.files_seen} files
          </div>
        ) : null
      }
    >
      <table className="hq-table w-full text-sm">
        <thead className="text-left text-slate-500">
          <tr>
            <th className="px-5 py-2">Strategy</th>
            <th>Day status</th>
            <th>Last engine log line</th>
            <th>Log synced to GCS</th>
            <th>Loaded into DB</th>
            <th>Parse</th>
            <th className="pr-5">Log vs DAILY SUMMARY</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((f) => {
            const c = f.checks;
            const reconciled = c?.summary_present ? c.closed_match !== false && c.net_match !== false : null;
            return (
              <tr key={f.strategy_code} className="border-t border-slate-800">
                <td className="px-5 py-1.5">{f.display_name}</td>
                <td>{f.paper_status ? <Chip value={f.paper_status} /> : <span className="text-slate-500">no log</span>}</td>
                <td>
                  {stamp(f.last_log_line_ts)} <span className="text-xs text-slate-500">{ago(f.last_log_line_ts)}</span>
                </td>
                <td>
                  {stamp(f.gcs_updated_at)} <span className="text-xs text-slate-500">{ago(f.gcs_updated_at)}</span>
                </td>
                <td>
                  {stamp(f.file_loaded_at ?? f.paper_loaded_at)}{" "}
                  <span className="text-xs text-slate-500">{ago(f.file_loaded_at ?? f.paper_loaded_at)}</span>
                </td>
                <td>
                  <Chip value={f.parse_status} />
                  {c?.unparsed_lines ? <span className="ml-1 text-xs text-amber-300">{c.unparsed_lines} unparsed</span> : null}
                </td>
                <td className="pr-5">
                  {reconciled == null ? (
                    <span className="text-xs text-slate-500">{f.paper_status === "INTRADAY" ? "session still open" : "—"}</span>
                  ) : reconciled ? (
                    <span className="text-emerald-300">
                      matches ({c!.closed_reported} trades, {usd(c!.net_reported)})
                    </span>
                  ) : (
                    <span className="text-rose-300">
                      stored {c!.trades_parsed} / {usd(c!.net_parsed)} vs log {c!.closed_reported} / {usd(c!.net_reported)}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Section>
  );
}

function DayTiles({ d }: { d: HqPaperDay }) {
  const trades = d.trades ?? [];
  const closed = trades.filter((t) => t.is_closed !== false && t.exit_ts);
  const open = trades.length - closed.length;
  const sum = (k: "net_pnl" | "gross_pnl" | "commission" | "slippage_cost") =>
    closed.reduce((a, t) => a + (t[k] ?? 0), 0);
  const net = sum("net_pnl");
  const wins = closed.filter((t) => (t.net_pnl ?? 0) > 0).length;
  const holds = closed.filter((t) => t.hold_min != null);
  const avgHold = holds.length ? holds.reduce((a, t) => a + (t.hold_min ?? 0), 0) / holds.length : null;
  const signals = d.signals ?? [];
  const blocked = signals.filter((s) => s.outcome !== "ENTERED").length;
  const fresh = d.freshness ?? [];
  const cap = fresh.reduce((a, f) => a + (f.daily_loss_cap ?? 0), 0);
  const capital = fresh.reduce((a, f) => a + (f.starting_capital ?? 0), 0);
  const warnEvents = (d.events ?? []).filter((e) => e.severity !== "INFO").length;
  return (
    <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
      <Tile
        label="Net P&L"
        value={usd(net)}
        tone={signTone(net)}
        sub={capital ? `${frac(net / capital)} of capital` : undefined}
      />
      <Tile
        label="Trades"
        value={`${closed.length}`}
        sub={`${open ? `${open} still open` : "all closed"}${avgHold != null ? ` · avg hold ${Math.round(avgHold)}m` : ""}`}
      />
      <Tile label="Win rate" value={closed.length ? frac(wins / closed.length, 0) : "—"} sub={`${wins} of ${closed.length} won`} />
      <Tile label="Gross P&L" value={usd(sum("gross_pnl"))} tone={signTone(sum("gross_pnl"))} />
      <Tile label="Commission" value={usd(sum("commission"))} />
      <Tile label="Slippage cost" value={usd(sum("slippage_cost"))} sub="vs signal prices" />
      <Tile
        label="Loss cap used"
        value={cap && net < 0 ? frac(net / cap, 0) : "0%"}
        tone={cap && net <= cap ? "text-rose-400" : undefined}
        sub={cap ? `cap ${usd(cap)}` : undefined}
      />
      <Tile
        label="Signals"
        value={`${signals.length}`}
        sub={`${signals.length - blocked} entered · ${blocked} blocked${warnEvents ? ` · ${warnEvents} warnings` : ""}`}
      />
    </div>
  );
}

function TradesTable({ d, showStrategy }: { d: HqPaperDay; showStrategy: boolean }) {
  const trades = d.trades ?? [];
  let running = 0;
  return (
    <Section title="Paper trades" subtitle={`${trades.length} trade(s) · times in ET · slippage = fill vs signal price`}>
      <table className="hq-table w-full text-sm">
        <thead className="text-left text-slate-500">
          <tr>
            {showStrategy ? <th className="px-5 py-2">Strategy</th> : null}
            <th className={showStrategy ? "" : "px-5 py-2"}>#</th>
            <th>Contract</th>
            <th>Side</th>
            <th>Qty</th>
            <th>Signal</th>
            <th>Entry fill</th>
            <th>Entry slip</th>
            <th>Latency</th>
            <th>Exit fill</th>
            <th>Exit slip</th>
            <th>Exit reason</th>
            <th>Points</th>
            <th>Gross</th>
            <th>Comm.</th>
            <th>Slip $</th>
            <th>Net</th>
            <th>Running net</th>
            <th>Held</th>
            <th className="pr-5">MAE / MFE</th>
          </tr>
        </thead>
        <tbody>
          {trades.length === 0 ? (
            <tr className="border-t border-slate-800">
              <td colSpan={20} className="px-5 py-2 text-slate-500">
                No paper trades this session.
              </td>
            </tr>
          ) : (
            trades.map((t) => {
              const isOpen = t.is_closed === false || !t.exit_ts;
              running += t.net_pnl ?? 0;
              return (
                <tr key={`${t.strategy_code}-${t.entry_ts}`} className="border-t border-slate-800">
                  {showStrategy ? <td className="px-5 py-1.5">{t.strategy_code}</td> : null}
                  <td className={showStrategy ? "" : "px-5 py-1.5"}>{t.trade_no}</td>
                  <td>{t.instrument ?? "—"}</td>
                  <td>{t.direction}</td>
                  <td>{t.contracts}</td>
                  <td>
                    {time(t.signal_bar_ts)} @ {num(t.entry_signal_px)}
                  </td>
                  <td>
                    {time(t.entry_ts)} @ {num(t.entry_px)}
                  </td>
                  <td>{num(t.entry_slip_pts)}</td>
                  <td>{t.fill_latency_s != null ? `${t.fill_latency_s.toFixed(0)}s` : "—"}</td>
                  <td>{isOpen ? <Chip value="OPEN" /> : `${time(t.exit_ts)} @ ${num(t.exit_px)}`}</td>
                  <td>{num(t.exit_slip_pts)}</td>
                  <td>{t.exit_reason ?? "—"}</td>
                  <td className={signTone(t.points)}>{num(t.points)}</td>
                  <td className={signTone(t.gross_pnl)}>{usd(t.gross_pnl)}</td>
                  <td>{usd(t.commission)}</td>
                  <td>{usd(t.slippage_cost)}</td>
                  <td className={`font-medium ${signTone(t.net_pnl)}`}>{usd(t.net_pnl)}</td>
                  <td className={signTone(running)}>{isOpen ? "—" : usd(running)}</td>
                  <td>{t.hold_min != null ? `${Math.round(t.hold_min)}m` : "—"}</td>
                  <td className="pr-5">
                    {num(t.mae_pts)} / {num(t.mfe_pts)}
                  </td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </Section>
  );
}

function SignalsTable({ d, showStrategy }: { d: HqPaperDay; showStrategy: boolean }) {
  const rows = d.signals ?? [];
  return (
    <Section title="Signals" subtitle="Every signal the engine produced and what it did with it">
      <table className="hq-table w-full text-sm">
        <thead className="text-left text-slate-500">
          <tr>
            <th className="px-5 py-2">Bar (ET)</th>
            {showStrategy ? <th>Strategy</th> : null}
            <th>Side</th>
            <th>Signal price</th>
            <th>Outcome</th>
            <th className="pr-5">Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr className="border-t border-slate-800">
              <td colSpan={6} className="px-5 py-2 text-slate-500">
                No signals this session.
              </td>
            </tr>
          ) : (
            rows.map((s) => (
              <tr key={`${s.strategy_code}-${s.signal_bar_ts}-${s.side}`} className="border-t border-slate-800">
                <td className="px-5 py-1.5">{time(s.signal_bar_ts)}</td>
                {showStrategy ? <td>{s.strategy_code}</td> : null}
                <td>{s.side}</td>
                <td>{num(s.signal_px)}</td>
                <td>
                  <Chip value={s.outcome === "ENTERED" ? "OK" : "AMBER"} title={s.outcome} />{" "}
                  <span className="text-xs text-slate-400">{s.outcome}</span>
                </td>
                <td className="wrap-cell pr-5 text-slate-400">{s.block_reason ?? "—"}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </Section>
  );
}

function FillsTable({ d, showStrategy }: { d: HqPaperDay; showStrategy: boolean }) {
  const rows = d.fills ?? [];
  return (
    <Section title="IB executions" subtitle={`${rows.length} fill(s) reported by Interactive Brokers`}>
      <table className="hq-table w-full text-sm">
        <thead className="text-left text-slate-500">
          <tr>
            <th className="px-5 py-2">Time (ET)</th>
            {showStrategy ? <th>Strategy</th> : null}
            <th>Contract</th>
            <th>Action</th>
            <th>Qty</th>
            <th>Price</th>
            <th>Commission</th>
            <th>Role</th>
            <th className="pr-5">Order / exec id</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr className="border-t border-slate-800">
              <td colSpan={9} className="px-5 py-2 text-slate-500">
                No fills this session.
              </td>
            </tr>
          ) : (
            rows.map((f, i) => (
              <tr key={`${f.strategy_code}-${f.fill_ts}-${i}`} className="border-t border-slate-800">
                <td className="px-5 py-1.5">{time(f.fill_ts)}</td>
                {showStrategy ? <td>{f.strategy_code}</td> : null}
                <td>{f.instrument ?? "—"}</td>
                <td className={f.action === "BUY" ? "text-emerald-300" : "text-rose-300"}>{f.action}</td>
                <td>{f.quantity}</td>
                <td>{num(f.price)}</td>
                <td>{usd(f.commission)}</td>
                <td>{f.leg_role ?? "—"}</td>
                <td className="pr-5 text-slate-400">{[f.order_ref, f.exec_id].filter(Boolean).join(" · ") || "—"}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </Section>
  );
}

function EventsTable({ d, showStrategy }: { d: HqPaperDay; showStrategy: boolean }) {
  const [all, setAll] = useState(false);
  const rows = d.events ?? [];
  const important = rows.filter((e) => e.severity !== "INFO" || e.category === "RISK");
  const shown = all ? rows : important;
  return (
    <Section
      title="Engine events"
      subtitle={`${rows.length} event(s): broker, connectivity, risk and system messages from the engine log`}
      right={
        <button
          onClick={() => setAll(!all)}
          className="rounded-lg border border-slate-700 px-3 py-1 text-xs text-slate-300 hover:bg-slate-800"
        >
          {all ? "Warnings and risk only" : `Show all ${rows.length}`}
        </button>
      }
    >
      <table className="hq-table w-full text-sm">
        <thead className="text-left text-slate-500">
          <tr>
            <th className="px-5 py-2">Time (ET)</th>
            {showStrategy ? <th>Strategy</th> : null}
            <th>Severity</th>
            <th>Category</th>
            <th>Code</th>
            <th className="pr-5">Message</th>
          </tr>
        </thead>
        <tbody>
          {shown.length === 0 ? (
            <tr className="border-t border-slate-800">
              <td colSpan={6} className="px-5 py-2 text-slate-500">
                {rows.length ? "No warnings or risk events. Use “Show all” for routine messages." : "No engine events."}
              </td>
            </tr>
          ) : (
            shown.map((e, i) => (
              <tr key={`${e.strategy_code}-${e.event_ts}-${i}`} className="border-t border-slate-800">
                <td className="px-5 py-1.5">{time(e.event_ts)}</td>
                {showStrategy ? <td>{e.strategy_code}</td> : null}
                <td>
                  <Chip value={e.severity === "ERROR" ? "RED" : e.severity === "WARN" ? "AMBER" : "INFO"} title={e.severity} />{" "}
                  <span className="text-xs text-slate-400">{e.severity}</span>
                </td>
                <td>{e.category}</td>
                <td>{e.code ?? "—"}</td>
                <td className="wrap-cell pr-5 text-slate-300">{e.message}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </Section>
  );
}

export function PaperDay({ strategy, date }: { strategy: HqStrategy | undefined; date: string }) {
  const code = strategy?.strategy_code;
  const q = useQuery({
    queryKey: ["hq-paper", code ?? "all", date],
    queryFn: () => getHqPaper(code, date),
    refetchInterval: REFRESH_MS,
  });
  if (q.isLoading || q.isError) return <QueryGate isLoading={q.isLoading} isError={q.isError} error={q.error} />;
  const d = q.data!;
  const showStrategy = !code;
  return (
    <>
      <div className="mb-3 text-xs text-slate-500">
        Page data fetched {stamp(new Date(q.dataUpdatedAt).toISOString())} · refreshes every 5 minutes
      </div>
      <Freshness rows={d.freshness ?? []} load={d.last_paper_load} />
      <DayTiles d={d} />
      <TradesTable d={d} showStrategy={showStrategy} />
      <div className="grid gap-x-4 xl:grid-cols-2">
        <SignalsTable d={d} showStrategy={showStrategy} />
        <FillsTable d={d} showStrategy={showStrategy} />
      </div>
      <EventsTable d={d} showStrategy={showStrategy} />
    </>
  );
}
