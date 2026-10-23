"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import QueryGate from "@/components/query-gate";
import {
  apiErrorMessage,
  approveFactoryTicket,
  getFactoryLine,
  rejectFactoryTicket,
  scanFactory,
  setFactoryStandingApproval,
} from "@/lib/api";
import type { FactoryStation, FactoryStrategy, FactoryTicket } from "@/lib/types";

const STATION_ORDER = [
  "paper",
  "daily_backtest",
  "compare",
  "bugs",
  "approval",
  "implement",
];

function stateClass(state: string) {
  if (state === "armed" || state === "clear" || state === "done") {
    return "bg-emerald-600/20 text-emerald-300";
  }
  if (state === "waiting" || state === "gap" || state === "flag" || state === "mixed") {
    return "bg-amber-600/20 text-amber-200";
  }
  if (state === "halted") return "bg-rose-600/20 text-rose-300";
  return "bg-slate-800 text-slate-400";
}

function statusClass(status: string) {
  if (status === "implemented") return "bg-emerald-600/20 text-emerald-300";
  if (status === "continued") return "bg-sky-600/20 text-sky-200";
  if (status === "rejected") return "bg-slate-800 text-slate-400";
  return "bg-amber-600/20 text-amber-200";
}

function statusLabel(status: string) {
  if (status === "pending_approval") return "Waiting on you";
  if (status === "implemented") return "Implemented";
  if (status === "continued") return "Continued, proposal only";
  if (status === "rejected") return "Rejected";
  return status;
}

function stationLabel(station: string) {
  if (station === "approval") return "At the approval gate";
  if (station === "continue") return "Continuing";
  if (station === "paper") return "On paper trade";
  return station;
}

function StationCard({ station, index }: { station: FactoryStation; index: number }) {
  return (
    <div className="card p-4">
      <div className="text-xs text-slate-500">Station {index}</div>
      <div className="mt-1 font-medium">{station.label}</div>
      <div className={`mt-2 inline-block rounded px-2 py-0.5 text-xs ${stateClass(station.state)}`}>
        {station.state}
      </div>
      <p className="mt-2 text-sm text-slate-400">{station.detail}</p>
    </div>
  );
}

function StrategyChip({ row }: { row: FactoryStrategy }) {
  return (
    <div className="rounded-lg border border-slate-800 px-3 py-2 text-sm">
      <div className="font-medium">{row.strategy_code}</div>
      <div className="text-xs text-slate-500">
        {row.paper}
        {row.account_id ? ` · ${row.account_id}` : ""}
        {row.process_running ? " · process up" : ""}
      </div>
      <div className="mt-1 text-xs text-slate-300">
        {stationLabel(row.station)}
        {row.open_bugs ? ` · ${row.open_bugs} open` : ""}
      </div>
    </div>
  );
}

function TicketCard({
  ticket,
  busy,
  onApprove,
  onReject,
}: {
  ticket: FactoryTicket;
  busy: boolean;
  onApprove: (historical: boolean) => void;
  onReject: () => void;
}) {
  const pending = ticket.status === "pending_approval";
  const trades = ticket.evidence.live_trades;
  const btTrades = ticket.evidence.bt_trades;
  const delta = ticket.evidence.pnl_delta;
  return (
    <article className="card p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-medium">{ticket.title}</h2>
          <div className="mt-1 text-sm text-slate-400">
            {ticket.strategy_code} · {ticket.session_date} · {ticket.ticket_id}
          </div>
        </div>
        <span className={`rounded px-2 py-1 text-xs ${statusClass(ticket.status)}`}>
          {statusLabel(ticket.status)}
        </span>
      </div>
      <p className="mt-3 text-sm text-slate-300">{ticket.detail}</p>
      <div className="mt-3 rounded-lg bg-slate-900/60 p-3 text-sm text-slate-200">
        <div className="text-xs uppercase tracking-wide text-slate-500">Suggested fix</div>
        <p className="mt-1">{ticket.suggested_fix}</p>
      </div>
      <div className="mt-3 text-xs text-slate-500">
        {trades != null || btTrades != null
          ? `Trades ${String(trades ?? "—")} live vs ${String(btTrades ?? "—")} backtest. `
          : ""}
        {delta != null ? `P&L delta ${String(delta)}. ` : ""}
        Thresholds locked.
      </div>
      {ticket.historical_backtest && (
        <p className="mt-2 text-sm text-slate-300">
          {ticket.historical_backtest.ok
            ? `Historical backtest ${ticket.historical_backtest.run_id} (live parity).`
            : `Historical backtest did not finish: ${ticket.historical_backtest.error}`}
        </p>
      )}
      {ticket.implementation && (
        <p className="mt-2 text-sm text-slate-300">
          {ticket.implementation.follow_up || ticket.implementation.blocked_reason}
          {ticket.approved_by ? ` · ${ticket.approved_by}` : ""}
          {ticket.approval_note ? ` · ${ticket.approval_note}` : ""}
        </p>
      )}
      {pending && (
        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onApprove(false)}
            className="rounded-lg bg-emerald-600 px-3 py-2 text-sm font-medium hover:bg-emerald-500 disabled:opacity-50"
          >
            Approve and continue
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onApprove(true)}
            className="rounded-lg bg-sky-700 px-3 py-2 text-sm font-medium hover:bg-sky-600 disabled:opacity-50"
          >
            Approve and run historical backtest
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onReject}
            className="rounded-lg bg-slate-800 px-3 py-2 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-50"
          >
            Reject
          </button>
        </div>
      )}
    </article>
  );
}

export default function Page() {
  const queryClient = useQueryClient();
  const line = useQuery({ queryKey: ["factory-line"], queryFn: getFactoryLine });
  const [note, setNote] = useState("");
  const [actionError, setActionError] = useState("");

  const scan = useMutation({
    mutationFn: scanFactory,
    onSuccess: async (data) => {
      setActionError("");
      queryClient.setQueryData(["factory-line"], data);
    },
    onError: (error) => setActionError(apiErrorMessage(error)),
  });

  const standing = useMutation({
    mutationFn: (enabled: boolean) => setFactoryStandingApproval(enabled, note),
    onSuccess: async (data) => {
      setActionError("");
      queryClient.setQueryData(["factory-line"], data);
    },
    onError: (error) => setActionError(apiErrorMessage(error)),
  });

  const decide = useMutation({
    mutationFn: async (input: { id: string; kind: "approve" | "reject"; historical: boolean }) => {
      if (input.kind === "reject") return rejectFactoryTicket(input.id, note);
      return approveFactoryTicket(input.id, input.historical, note);
    },
    onSuccess: async () => {
      setActionError("");
      await queryClient.invalidateQueries({ queryKey: ["factory-line"] });
    },
    onError: (error) => setActionError(apiErrorMessage(error)),
  });

  if (line.isLoading || line.isError || !line.data) {
    return <QueryGate isLoading={line.isLoading} isError={line.isError} error={line.error} />;
  }

  const data = line.data;
  const stations = STATION_ORDER.map((id) => data.stations.find((station) => station.id === id)).filter(
    (station): station is FactoryStation => Boolean(station),
  );
  const pending = data.tickets.filter((ticket) => ticket.status === "pending_approval");
  const closed = data.tickets.filter((ticket) => ticket.status !== "pending_approval");

  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Strategy test line</h1>
          <p className="mt-2 max-w-3xl text-sm text-slate-400">
            Paper trade keeps running. Each pass compares it with the daily backtest and opens a bug
            when they disagree. Standing approval implements the safe fix, runs a historical
            backtest for that session, and continues. Locked thresholds are not changed.
          </p>
        </div>
        <button
          type="button"
          disabled={scan.isPending}
          onClick={() => scan.mutate()}
          className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium hover:bg-emerald-500 disabled:opacity-50"
        >
          {scan.isPending ? "Scanning…" : "Scan daily compare"}
        </button>
      </div>

      {data.thresholds_locked && (
        <div className="mb-4 text-xs text-emerald-300">Signal, stop, and profit-take thresholds are locked.</div>
      )}
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-800 px-4 py-3">
        <p className="text-sm text-slate-300">
          {data.standing_approval?.enabled
            ? "Standing approval is on. Safe fixes are implemented, a historical backtest is attempted, and the line continues."
            : "Standing approval is off. Each mismatch waits for a person."}
        </p>
        <button
          type="button"
          disabled={standing.isPending}
          onClick={() => standing.mutate(!data.standing_approval?.enabled)}
          className="rounded-lg bg-slate-800 px-3 py-2 text-sm text-slate-100 hover:bg-slate-700 disabled:opacity-50"
        >
          {data.standing_approval?.enabled ? "Revoke standing approval" : "Grant standing approval"}
        </button>
      </div>
      {actionError && <div className="mb-4 text-sm text-rose-300">{actionError}</div>}
      {data.source === "no_hq_database" && (
        <div className="mb-4 text-sm text-amber-200">
          The HQ database is not connected, so this scan did not see stored daily sync rows.
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {stations.map((station, index) => (
          <StationCard key={station.id} station={station} index={index + 1} />
        ))}
      </div>

      <h2 className="mb-3 mt-8 text-sm uppercase tracking-wide text-slate-500">Strategies on the line</h2>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {data.strategies.map((row) => (
          <StrategyChip key={row.strategy_code} row={row} />
        ))}
      </div>

      <div className="mb-4 mt-8">
        <label className="text-xs uppercase tracking-wide text-slate-500" htmlFor="factory-note">
          Note for the next approval
        </label>
        <input
          id="factory-note"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="Optional note kept on the ticket"
          className="mt-2 w-full rounded-lg border border-slate-800 bg-slate-950 px-3 py-2 text-sm"
        />
      </div>

      <h2 className="mb-3 text-sm uppercase tracking-wide text-slate-500">
        Open bugs ({pending.length})
      </h2>
      <div className="flex flex-col gap-4">
        {pending.length === 0 && (
          <div className="card p-5 text-sm text-slate-400">
            No mismatch is waiting. Scan reads the daily sync log and opens a ticket only when paper
            and the backtest disagree.
          </div>
        )}
        {pending.map((ticket) => (
          <TicketCard
            key={ticket.ticket_id}
            ticket={ticket}
            busy={decide.isPending}
            onApprove={(historical) =>
              decide.mutate({ id: ticket.ticket_id, kind: "approve", historical })
            }
            onReject={() =>
              decide.mutate({ id: ticket.ticket_id, kind: "reject", historical: false })
            }
          />
        ))}
      </div>

      {closed.length > 0 && (
        <>
          <h2 className="mb-3 mt-8 text-sm uppercase tracking-wide text-slate-500">
            Already decided ({closed.length})
          </h2>
          <div className="flex flex-col gap-4">
            {closed.map((ticket) => (
              <TicketCard
                key={ticket.ticket_id}
                ticket={ticket}
                busy={false}
                onApprove={() => undefined}
                onReject={() => undefined}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
