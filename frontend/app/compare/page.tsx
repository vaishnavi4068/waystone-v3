"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarDays, GitCompare } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";

import QueryGate from "@/components/query-gate";
import StagedBanner from "@/components/staged-banner";
import {
  apiErrorMessage,
  createAlgo,
  deleteAlgo,
  getAlgoCompare,
  getAlgos,
  getCompareDays,
  isNotFound,
} from "@/lib/api";
import { contractLabel, money, tone } from "@/lib/format";
import type { AlgoCompare, AlgoOnboard, CompareRow } from "@/lib/types";

const ID_RE = /^[a-z][a-z0-9_]{1,47}$/;

type StatusFilter = "all" | "matched" | "live_only" | "replay_only";

function signedMoney(n: number) {
  return `${n > 0 ? "+" : ""}${money(n)}`;
}

function sourceLabel(raw: string) {
  if (raw === "algo_live") return "algo live folder";
  if (raw === "algo_replay") return "algo replay folder";
  if (raw === "ibkr_dump") return "shared IBKR dump";
  if (raw === "missing") return "missing";
  return raw;
}

function statusClass(status: string) {
  if (status === "matched") return "bg-emerald-600/20 text-emerald-300";
  if (status === "live_only") return "bg-amber-600/20 text-amber-200";
  if (status === "replay_only") return "bg-sky-600/20 text-sky-200";
  return "bg-slate-800 text-slate-400";
}

function statusLabel(status: string) {
  if (status === "matched") return "Matched";
  if (status === "live_only") return "Live only";
  if (status === "replay_only") return "Replay only";
  return status;
}

function sideClass(side: string) {
  const token = side.toUpperCase();
  return token === "BOT" || token === "BUY" ? "text-emerald-400" : "text-rose-400";
}

function Stat({
  label,
  value,
  className = "",
}: {
  label: string;
  value: string;
  className?: string;
}) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${className}`}>{value}</div>
    </div>
  );
}

function BookPanel({
  title,
  source,
  fills,
  qty,
  notional,
  commission,
  pnl,
}: {
  title: string;
  source: string;
  fills: number;
  qty: number;
  notional: number;
  commission: number;
  pnl: number;
}) {
  return (
    <div className="card p-5">
      <div className="mb-1 font-medium">{title}</div>
      <div className="mb-4 text-xs text-slate-500">{sourceLabel(source)}</div>
      <div className="grid grid-cols-2 gap-4">
        <Stat label="Fills" value={String(fills)} />
        <Stat label="Qty" value={String(qty)} />
        <Stat label="Notional" value={money(notional)} />
        <Stat label="Commission" value={money(commission)} />
        <Stat label="Realized P&L" value={money(pnl)} className={tone(pnl)} />
      </div>
    </div>
  );
}

function emptyForm(): AlgoOnboard {
  return {
    id: "",
    name: "",
    book: "futures",
    live_prefix: "",
    replay_prefix: "",
    client_id: null,
    notes: "",
  };
}

export default function Page() {
  const queryClient = useQueryClient();
  const algos = useQuery({ queryKey: ["algos"], queryFn: getAlgos });
  const days = useQuery({ queryKey: ["compare-days"], queryFn: getCompareDays });
  const [date, setDate] = useState("");
  const [algoId, setAlgoId] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [form, setForm] = useState<AlgoOnboard>(emptyForm);
  const [formError, setFormError] = useState("");

  useEffect(() => {
    if (days.data?.latest && !date) setDate(days.data.latest);
  }, [days.data, date]);

  useEffect(() => {
    if (algos.data?.algos.length && !algoId) setAlgoId(algos.data.algos[0].id);
  }, [algos.data, algoId]);

  const compare = useQuery({
    queryKey: ["algo-compare", algoId, date],
    queryFn: () => getAlgoCompare(algoId, date),
    enabled: Boolean(algoId && date),
  });

  const onboard = useMutation({
    mutationFn: createAlgo,
    onSuccess: async (created) => {
      setForm(emptyForm());
      setFormError("");
      setAlgoId(created.id);
      await queryClient.invalidateQueries({ queryKey: ["algos"] });
      await queryClient.invalidateQueries({ queryKey: ["compare-days"] });
    },
    onError: (error) => setFormError(apiErrorMessage(error)),
  });

  const remove = useMutation({
    mutationFn: deleteAlgo,
    onSuccess: async (_ok, removedId) => {
      await queryClient.invalidateQueries({ queryKey: ["algos"] });
      if (algoId === removedId) {
        const next = algos.data?.algos.find((row) => row.id !== removedId);
        setAlgoId(next?.id ?? "");
      }
    },
  });

  const rows = useMemo(() => {
    const all: CompareRow[] = compare.data?.rows ?? [];
    if (status === "all") return all;
    return all.filter((row) => row.status === status);
  }, [compare.data, status]);

  if (algos.isLoading || days.isLoading) {
    return <QueryGate isLoading isError={false} />;
  }
  if ((algos.isError && !isNotFound(algos.error)) || (days.isError && !isNotFound(days.error))) {
    return <QueryGate isLoading={false} isError error={algos.error ?? days.error} />;
  }
  if (algos.isError || days.isError || !algos.data || !days.data) {
    return (
      <div className="card p-5 text-sm text-slate-400">
        IBKR reports are not configured, or no live/replay blotter has been published. Seed demo
        data with <code className="text-slate-200">waystone3 ibkr-seed-demo</code> or publish
        blotters under <code className="text-slate-200">algos/v1/&lt;id&gt;/live</code> and{" "}
        <code className="text-slate-200">replay</code>.
      </div>
    );
  }

  const selected = algos.data.algos.find((row) => row.id === algoId);
  const payload: AlgoCompare | undefined = compare.data;

  function submitOnboard(event: FormEvent) {
    event.preventDefault();
    if (!ID_RE.test(form.id)) {
      setFormError("Id must be lowercase letters, digits, underscore (start with a letter).");
      return;
    }
    if (!form.name.trim()) {
      setFormError("Name is required.");
      return;
    }
    setFormError("");
    onboard.mutate({
      id: form.id.trim(),
      name: form.name.trim(),
      book: form.book,
      live_prefix: form.live_prefix?.trim() || "",
      replay_prefix: form.replay_prefix?.trim() || "",
      client_id: form.client_id == null || Number.isNaN(form.client_id) ? null : form.client_id,
      notes: form.notes?.trim() || "",
    });
  }

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Daily Compare</h1>
          <p className="mt-1 text-sm text-slate-500">
            Same-day live paper blotter vs replay backtest for each onboarded algo.
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm text-slate-400">
          <CalendarDays size={16} />
          <select
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-200"
          >
            {days.data.days.length === 0 && <option value="">No published days</option>}
            {days.data.days
              .slice()
              .reverse()
              .map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
          </select>
        </label>
      </div>

      <div className="mb-6 flex flex-wrap gap-2">
        {algos.data.algos.map((algo) => (
          <button
            key={algo.id}
            type="button"
            onClick={() => setAlgoId(algo.id)}
            className={`rounded-lg px-3 py-1.5 text-sm ${
              algoId === algo.id ? "bg-emerald-600/20 text-emerald-300" : "bg-slate-800 text-slate-300"
            }`}
          >
            {algo.name}
            <span className="ml-2 text-xs capitalize text-slate-500">{algo.book}</span>
          </button>
        ))}
      </div>

      {(days.data.staged || payload?.staged) && (
        <StagedBanner week={payload?.staged_week ?? days.data.staged_week} />
      )}

      {selected && (
        <p className="mb-4 text-xs text-slate-500">
          Live <code className="text-slate-300">{selected.live_prefix || `algos/v1/${selected.id}/live`}</code>
          {" · "}
          Replay{" "}
          <code className="text-slate-300">
            {selected.replay_prefix || `algos/v1/${selected.id}/replay`}
          </code>
          {selected.client_id != null ? ` · clientId ${selected.client_id}` : ""}
          {selected.notes ? ` · ${selected.notes}` : ""}
        </p>
      )}

      {compare.isLoading && <div className="text-slate-400">Loading comparison…</div>}
      {compare.isError && <QueryGate isLoading={false} isError error={compare.error} />}

      {payload && (
        <>
          <div className="mb-4 flex flex-wrap gap-3 text-sm">
            <span className="rounded-lg bg-slate-800 px-3 py-1 text-slate-300">
              Matched {payload.matched}
            </span>
            <span className="rounded-lg bg-slate-800 px-3 py-1 text-slate-300">
              Live only {payload.live_only}
            </span>
            <span className="rounded-lg bg-slate-800 px-3 py-1 text-slate-300">
              Replay only {payload.replay_only}
            </span>
            <span className="rounded-lg bg-slate-800 px-3 py-1 text-slate-300">
              Avg price Δ{" "}
              {payload.avg_price_delta == null ? "—" : payload.avg_price_delta.toFixed(4)}
            </span>
            <span className="rounded-lg bg-slate-800 px-3 py-1 text-slate-300">
              Avg P&L Δ{" "}
              {payload.avg_pnl_delta == null ? "—" : signedMoney(payload.avg_pnl_delta)}
            </span>
          </div>

          <div className="mb-6 grid grid-cols-1 gap-4 lg:grid-cols-3">
            <BookPanel
              title="Live paper"
              source={payload.live_source}
              fills={payload.live.fills}
              qty={payload.live.qty}
              notional={payload.live.notional}
              commission={payload.live.commission}
              pnl={payload.live.realized_pnl}
            />
            <BookPanel
              title="Replay backtest"
              source={payload.replay_source}
              fills={payload.replay.fills}
              qty={payload.replay.qty}
              notional={payload.replay.notional}
              commission={payload.replay.commission}
              pnl={payload.replay.realized_pnl}
            />
            <div className="card p-5">
              <div className="mb-1 flex items-center gap-2 font-medium">
                <GitCompare size={16} />
                Delta (live − replay)
              </div>
              <div className="mb-4 text-xs text-slate-500">Positive means live is larger.</div>
              <div className="grid grid-cols-2 gap-4">
                <Stat label="Fills" value={String(payload.deltas.fills)} />
                <Stat label="Qty" value={String(payload.deltas.qty)} />
                <Stat label="Notional" value={signedMoney(payload.deltas.notional)} />
                <Stat label="Commission" value={signedMoney(payload.deltas.commission)} />
                <Stat
                  label="Realized P&L"
                  value={signedMoney(payload.deltas.realized_pnl)}
                  className={tone(payload.deltas.realized_pnl)}
                />
              </div>
            </div>
          </div>

          <div className="mb-3 flex gap-2 text-sm">
            {(["all", "matched", "live_only", "replay_only"] as const).map((key) => (
              <button
                key={key}
                type="button"
                onClick={() => setStatus(key)}
                className={`rounded-lg px-3 py-1.5 ${
                  status === key ? "bg-emerald-600/20 text-emerald-300" : "bg-slate-800 text-slate-300"
                }`}
              >
                {key === "all" ? "All rows" : statusLabel(key)}
              </button>
            ))}
          </div>

          {rows.length === 0 ? (
            <div className="card p-5 text-sm text-slate-500">No fills for this filter.</div>
          ) : (
            <div className="card mb-8 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-slate-900/60 text-left text-slate-500">
                  <tr>
                    <th className="px-5 py-3">Status</th>
                    <th>Contract</th>
                    <th>Side</th>
                    <th>Qty</th>
                    <th>Live px</th>
                    <th>Replay px</th>
                    <th>Px Δ</th>
                    <th>Live P&L</th>
                    <th>Replay P&L</th>
                    <th>P&L Δ</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, idx) => (
                    <tr key={`${row.status}-${row.symbol}-${row.side}-${idx}`} className="border-t border-slate-800">
                      <td className="px-5 py-3">
                        <span className={`rounded px-2 py-0.5 text-xs ${statusClass(row.status)}`}>
                          {statusLabel(row.status)}
                        </span>
                      </td>
                      <td className="font-medium">
                        {contractLabel(row)}
                        <div className="text-xs font-normal capitalize text-slate-500">{row.book}</div>
                      </td>
                      <td className={sideClass(row.side)}>{row.side}</td>
                      <td>{row.qty}</td>
                      <td>{row.live_price == null ? "—" : money(row.live_price)}</td>
                      <td>{row.replay_price == null ? "—" : money(row.replay_price)}</td>
                      <td className={row.price_delta == null ? "" : tone(row.price_delta)}>
                        {row.price_delta == null ? "—" : row.price_delta.toFixed(4)}
                      </td>
                      <td className={row.live_pnl == null ? "" : tone(row.live_pnl)}>
                        {row.live_pnl == null ? "—" : money(row.live_pnl)}
                      </td>
                      <td className={row.replay_pnl == null ? "" : tone(row.replay_pnl)}>
                        {row.replay_pnl == null ? "—" : money(row.replay_pnl)}
                      </td>
                      <td className={row.pnl_delta == null ? "" : tone(row.pnl_delta)}>
                        {row.pnl_delta == null ? "—" : signedMoney(row.pnl_delta)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <div className="mt-10 grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="card p-5">
          <h2 className="mb-1 text-lg font-medium">Onboard an algo</h2>
          <p className="mb-4 text-sm text-slate-500">
            Registers live IBKR log and replay folders. Leave prefixes blank to use{" "}
            <code className="text-slate-300">algos/v1/&lt;id&gt;/live</code> and{" "}
            <code className="text-slate-300">algos/v1/&lt;id&gt;/replay</code>.
          </p>
          <form onSubmit={submitOnboard} className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="text-sm text-slate-400">
              Id
              <input
                value={form.id}
                onChange={(e) => setForm({ ...form, id: e.target.value })}
                placeholder="cl_futures"
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-200"
              />
            </label>
            <label className="text-sm text-slate-400">
              Name
              <input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="CL futures"
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-200"
              />
            </label>
            <label className="text-sm text-slate-400">
              Book
              <select
                value={form.book}
                onChange={(e) => setForm({ ...form, book: e.target.value })}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-200"
              >
                <option value="futures">futures</option>
                <option value="options">options</option>
                <option value="other">other</option>
              </select>
            </label>
            <label className="text-sm text-slate-400">
              IBKR clientId (optional)
              <input
                type="number"
                value={form.client_id ?? ""}
                onChange={(e) =>
                  setForm({
                    ...form,
                    client_id: e.target.value === "" ? null : Number(e.target.value),
                  })
                }
                placeholder="e.g. 7"
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-200"
              />
            </label>
            <label className="text-sm text-slate-400 sm:col-span-2">
              Live prefix
              <input
                value={form.live_prefix ?? ""}
                onChange={(e) => setForm({ ...form, live_prefix: e.target.value })}
                placeholder="algos/v1/cl_futures/live"
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-200"
              />
            </label>
            <label className="text-sm text-slate-400 sm:col-span-2">
              Replay prefix
              <input
                value={form.replay_prefix ?? ""}
                onChange={(e) => setForm({ ...form, replay_prefix: e.target.value })}
                placeholder="algos/v1/cl_futures/replay"
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-200"
              />
            </label>
            <label className="text-sm text-slate-400 sm:col-span-2">
              Notes
              <input
                value={form.notes ?? ""}
                onChange={(e) => setForm({ ...form, notes: e.target.value })}
                placeholder="Paper CL. Replay from the backtest folder."
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-200"
              />
            </label>
            {formError && <div className="text-sm text-rose-300 sm:col-span-2">{formError}</div>}
            <div className="sm:col-span-2">
              <button
                type="submit"
                disabled={onboard.isPending}
                className="rounded-lg bg-emerald-600/20 px-4 py-2 text-sm text-emerald-300 hover:bg-emerald-600/30 disabled:opacity-50"
              >
                {onboard.isPending ? "Saving…" : "Register algo"}
              </button>
            </div>
          </form>
        </div>

        <div className="card p-5">
          <h2 className="mb-1 text-lg font-medium">Registered algos</h2>
          <p className="mb-4 text-sm text-slate-500">
            Three defaults ship with the demo seed. Add more here or with{" "}
            <code className="text-slate-300">waystone3 algo-register</code>.
          </p>
          <div className="space-y-3">
            {algos.data.algos.map((algo) => (
              <div key={algo.id} className="rounded-lg border border-slate-800 px-3 py-2">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="font-medium">
                      {algo.name}{" "}
                      <span className="text-xs font-normal capitalize text-slate-500">{algo.book}</span>
                    </div>
                    <div className="mt-1 text-xs text-slate-500">
                      {algo.id}
                      {algo.client_id != null ? ` · client ${algo.client_id}` : ""}
                    </div>
                    <div className="mt-1 break-all text-xs text-slate-500">
                      {algo.live_prefix} → {algo.replay_prefix}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => remove.mutate(algo.id)}
                    className="text-xs text-slate-500 hover:text-rose-300"
                  >
                    Remove
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
