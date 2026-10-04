"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { Chip, frac, signTone, stamp, usd } from "@/components/hq";
import QueryGate from "@/components/query-gate";
import { getHqStatus, getHqStrategies, getHqSync, isNotFound } from "@/lib/api";
import type { HqStrategy } from "@/lib/types";

function StrategyCard({ s }: { s: HqStrategy }) {
  const body = (
    <>
      <div className="mb-1 flex items-start justify-between gap-3">
        <div>
          <div className="font-medium">{s.display_name}</div>
          <div className="text-xs text-slate-500">
            {s.strategy_code} · {s.instrument_root ?? s.asset_class}
            {s.paper_start_date ? ` · paper since ${s.paper_start_date}` : ""}
          </div>
        </div>
        {!s.is_active ? <Chip value="INACTIVE" /> : null}
      </div>
      <div className="mt-3">
        <Chip wrap value={s.overall_gate ?? (s.asset_class === "future" ? "NO DATA YET" : "NOT LOADED")} />
      </div>
      <div className="mt-4 grid grid-cols-3 gap-2 text-sm">
        <div>
          <div className="text-xs text-slate-500">Net P&amp;L (ITD)</div>
          <div className={signTone(s.net_pnl)}>{usd(s.net_pnl)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">Return</div>
          <div className={signTone(s.return_pct)}>{frac(s.return_pct)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">Equity</div>
          <div>{usd(s.equity_end)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">Days / trades</div>
          <div>
            {s.trading_days ?? "—"} / {s.trades ?? "—"}
          </div>
        </div>
        <div className="col-span-2">
          <div className="text-xs text-slate-500">KPI status (R / A / G)</div>
          <div>
            <span className="text-rose-300">{s.red_count ?? 0}</span> /{" "}
            <span className="text-amber-200">{s.amber_count ?? 0}</span> /{" "}
            <span className="text-emerald-300">{s.green_count ?? 0}</span>
          </div>
        </div>
      </div>
      {s.last_session ? (
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-800 pt-3 text-xs text-slate-500">
          <span>{s.last_session}</span>
          <span>paper</span> <Chip value={s.paper_status} />
          <span>backtest</span> <Chip value={s.backtest_status} />
          <span>sync</span> <Chip value={s.sync_status} />
        </div>
      ) : null}
      {s.notes ? <p className="mt-3 text-xs text-slate-500">{s.notes}</p> : null}
    </>
  );
  if (s.asset_class !== "future") return <div className="card p-5 opacity-70">{body}</div>;
  return (
    <Link href={`/hq/${s.strategy_code}`} className="card block p-5 hover:border-slate-600">
      {body}
    </Link>
  );
}

function SyncTable() {
  const [date, setDate] = useState<string | undefined>();
  const sync = useQuery({ queryKey: ["hq-sync", date], queryFn: () => getHqSync(date) });
  if (sync.isLoading || sync.isError) {
    return <QueryGate isLoading={sync.isLoading} isError={sync.isError} error={sync.error} />;
  }
  const data = sync.data!;
  return (
    <div className="card mb-6 overflow-x-auto">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-5 py-3">
        <div className="font-medium">Live vs backtest · {data.session_date ?? "no sessions yet"}</div>
        {data.dates.length > 0 ? (
          <select
            className="rounded bg-slate-900 px-2 py-1 text-sm"
            value={data.session_date ?? ""}
            onChange={(e) => setDate(e.target.value)}
          >
            {[...data.dates].reverse().map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        ) : null}
      </div>
      <table className="hq-table w-full text-sm">
        <thead className="bg-slate-900/60 text-left text-slate-500">
          <tr>
            <th className="px-5 py-2">Strategy</th>
            <th>Live trades</th>
            <th>Live net</th>
            <th>Backtest trades</th>
            <th>Backtest net</th>
            <th>Δ $</th>
            <th>Δ %</th>
            <th>Exit match</th>
            <th>Paper</th>
            <th>Backtest</th>
            <th>Sync</th>
            <th className="pr-5">Notes</th>
          </tr>
        </thead>
        <tbody>
          {data.rows.map((r) => (
            <tr key={r.strategy_code} className="border-t border-slate-800 align-top">
              <td className="px-5 py-2">
                <Link className="hover:underline" href={`/hq/${r.strategy_code}?date=${r.session_date}`}>
                  {r.display_name}
                </Link>
                <div className="text-xs text-slate-500">{r.instrument_symbol}</div>
              </td>
              <td>{r.live_trades ?? "—"}</td>
              <td className={signTone(r.live_net_pnl)}>{usd(r.live_net_pnl)}</td>
              <td>{r.bt_trades ?? "—"}</td>
              <td className={signTone(r.bt_net_pnl)}>{usd(r.bt_net_pnl)}</td>
              <td className={signTone(r.pnl_delta)}>{usd(r.pnl_delta)}</td>
              <td>{frac(r.pnl_delta_pct, 1)}</td>
              <td>{r.exit_reason_match == null ? "—" : r.exit_reason_match ? "yes" : "no"}</td>
              <td><Chip value={r.paper_status} /></td>
              <td><Chip value={r.backtest_status} /></td>
              <td><Chip value={r.sync_status} /></td>
              <td className="wrap-cell max-w-xs pr-5 text-xs text-slate-500">
                {[r.loss_cap_hit ? "loss cap hit" : null, r.notes_auto, r.notes_manual].filter(Boolean).join(" · ") || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Page() {
  const strategies = useQuery({ queryKey: ["hq-strategies"], queryFn: getHqStrategies });
  const status = useQuery({ queryKey: ["hq-status"], queryFn: getHqStatus });

  if (strategies.isLoading) return <QueryGate isLoading isError={false} />;
  if (strategies.isError && isNotFound(strategies.error)) {
    return (
      <div className="card p-5 text-sm text-slate-400">
        The HQ database is not connected. Set <code className="text-slate-200">WAYSTONE_HQ_DB_HOST</code> and{" "}
        <code className="text-slate-200">WAYSTONE_HQ_DB_PASSWORD</code> (read-only user) on the API.
      </div>
    );
  }
  if (strategies.isError) return <QueryGate isLoading={false} isError error={strategies.error} />;

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-semibold">Futures HQ</h1>
        <p className="mt-1 text-sm text-slate-500">
          Paper trading vs backtest replay, daily P&amp;L and workbook KPIs, loaded from the VM logs into the HQ
          database. Paper logs load hourly at :35; backtest replays at 16:35 and 17:35 ET.
        </p>
      </div>
      <div className="mb-6 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {strategies.data!.strategies.map((s) => (
          <StrategyCard key={s.strategy_code} s={s} />
        ))}
      </div>
      <SyncTable />
      {status.data ? (
        <div className="card p-5 text-sm">
          <div className="mb-2 font-medium">Loader</div>
          {status.data.loads.length === 0 ? (
            <div className="text-slate-500">No loads recorded yet.</div>
          ) : (
            <div className="flex flex-wrap gap-6">
              {status.data.loads.map((l) => (
                <div key={l.job}>
                  <div className="text-xs uppercase text-slate-500">{l.job}</div>
                  <div className="mt-1 flex items-center gap-2">
                    <Chip value={l.status} title={l.error ?? undefined} />
                    <span className="text-slate-400">{stamp(l.started_at)}</span>
                  </div>
                  <div className="text-xs text-slate-500">
                    {l.files_loaded}/{l.files_seen} files loaded
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
