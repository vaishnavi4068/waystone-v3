"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, ExternalLink } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";

import { num, signTone, stamp, time } from "@/components/hq";
import { Section } from "@/components/hq-views";
import QueryGate from "@/components/query-gate";
import {
  getHqSentiment,
  getHqSentimentDay,
  getHqSentimentNow,
  getHqSentimentQuality,
  getHqSentimentSeries,
  getHqStrategies,
  isNotFound,
} from "@/lib/api";
import type {
  HqStrategy,
  SentimentDaySummary,
  SentimentEvent,
  SentimentGate,
  SentimentHeadline,
  SentimentPoint,
  SentimentRecommendation,
  SentimentScore,
  SentimentSnapshot,
  Verdict,
} from "@/lib/types";

const ALL = "all";
const DAY = "DAY";
const NOW_REFRESH_MS = 60 * 1000;
const GATE_ORDER = ["data", "event", "kill", "engine", "vol", "positioning"];
const GATE_LABEL: Record<string, string> = {
  data: "Data",
  event: "Event blackout",
  kill: "Kill switch",
  engine: "Engine (F&G × chop)",
  vol: "Vol regime",
  positioning: "Positioning (COT)",
};
const PRESETS = [7, 30, 90, 365];
const field = "rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100";

/* ------------------------------------------------------------------ helpers */

function gateClass(state?: string | null) {
  switch (state) {
    case "OPEN":
    case "TRADE":
    case "OK":
      return "bg-emerald-600/20 text-emerald-300";
    case "CAUTION":
    case "REDUCE":
    case "STALE":
      return "bg-amber-600/20 text-amber-200";
    case "HALT":
    case "BLOCKED":
    case "STAND_DOWN":
    case "FAILING":
      return "bg-rose-600/20 text-rose-300";
    default:
      return "bg-slate-800 text-slate-400";
  }
}

function Badge({ value, title }: { value?: string | null; title?: string }) {
  if (!value) return <span className="text-slate-600">—</span>;
  return (
    <span title={title} className={`whitespace-nowrap rounded px-2 py-0.5 text-xs ${gateClass(value)}`}>
      {value.replace("_", " ")}
    </span>
  );
}

function fngState(v?: number | null) {
  if (v == null) return "—";
  if (v <= 25) return "extreme fear";
  if (v <= 30) return "fear (gate)";
  if (v < 45) return "fear";
  if (v <= 55) return "neutral";
  if (v < 70) return "greed";
  return "extreme greed";
}

const fngTone = (v?: number | null) =>
  v == null ? "text-slate-500" : v <= 30 ? "text-rose-400" : v >= 70 ? "text-amber-300" : "text-slate-100";

const size = (m?: number | null) => (m == null ? "—" : `${m.toFixed(2)}×`);

function tierLabel(t: number) {
  if (t >= 1) return "wire";
  if (t > 0) return "aggregator";
  return "junk";
}

function addDays(d: string, n: number) {
  const t = new Date(`${d}T12:00:00Z`);
  t.setUTCDate(t.getUTCDate() + n);
  return t.toISOString().slice(0, 10);
}

function eventTs(e: SentimentEvent) {
  return e.event_ts ?? e.ts ?? null;
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-slate-800 p-3">
      <div className="text-xs text-slate-500">{label}</div>
      <div className={`text-lg font-semibold ${tone ?? ""}`}>{value}</div>
      {sub ? <div className="text-xs text-slate-500">{sub}</div> : null}
    </div>
  );
}

/* ---------------------------------------------------------------- selection */

interface Selection {
  strategy: string;
  date: string | null;
  from: string | undefined;
  to: string | undefined;
  dates: string[];
  futures: HqStrategy[];
  go: (next: Partial<{ strategy: string; date: string | null; from: string; to: string }>) => void;
}

function useSentimentSelection() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const strategy = params.get("strategy") ?? ALL;
  const requested = params.get("date");

  const strategies = useQuery({ queryKey: ["hq-strategies"], queryFn: getHqStrategies });
  const index = useQuery({ queryKey: ["hq-sentiment-dates"], queryFn: () => getHqSentiment(), refetchInterval: NOW_REFRESH_MS * 5 });
  const dates = index.data?.dates ?? [];
  const latest = dates[dates.length - 1] ?? null;
  const date = requested && dates.includes(requested) ? requested : latest;
  const to = params.get("to") ?? latest ?? undefined;
  const from = params.get("from") ?? (to ? addDays(to, -30) : undefined);

  function go(next: Partial<{ strategy: string; date: string | null; from: string; to: string }>) {
    const q = new URLSearchParams();
    const s = next.strategy ?? strategy;
    const d = next.date === undefined ? date : next.date;
    const f = next.from ?? params.get("from");
    const t = next.to ?? params.get("to");
    if (s !== ALL) q.set("strategy", s);
    if (d) q.set("date", d);
    if (f) q.set("from", f);
    if (t) q.set("to", t);
    router.replace(`${pathname}${q.toString() ? `?${q}` : ""}`, { scroll: false });
  }

  const futures = (strategies.data?.strategies ?? []).filter((s) => s.asset_class === "future");
  const selection: Selection = { strategy, date, from, to, dates, futures, go };
  return {
    selection,
    loading: strategies.isLoading || index.isLoading,
    error: index.error ?? strategies.error,
  };
}

function FilterBar({ s }: { s: Selection }) {
  const newestFirst = [...s.dates].reverse();
  const i = s.date ? newestFirst.indexOf(s.date) : -1;
  const older = i >= 0 ? newestFirst[i + 1] : undefined;
  const newer = i > 0 ? newestFirst[i - 1] : undefined;
  const latest = newestFirst[0];
  return (
    <div className="card sticky top-0 z-10 mb-6 flex flex-wrap items-end gap-6 px-5 py-4">
      <label className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-slate-500">Strategy</span>
        <select className={`${field} min-w-52`} value={s.strategy} onChange={(e) => s.go({ strategy: e.target.value })}>
          <option value={ALL}>All futures strategies</option>
          {s.futures.map((f) => (
            <option key={f.strategy_code} value={f.strategy_code}>
              {f.display_name}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-slate-500">Session</span>
        <div className="flex items-center gap-1">
          <button
            aria-label="Previous session"
            disabled={!older}
            onClick={() => older && s.go({ date: older })}
            className="rounded-lg border border-slate-700 p-2 text-slate-300 hover:bg-slate-800 disabled:opacity-30"
          >
            <ChevronLeft size={16} />
          </button>
          <select className={`${field} min-w-40`} value={s.date ?? ""} onChange={(e) => s.go({ date: e.target.value })}>
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
            onClick={() => newer && s.go({ date: newer })}
            className="rounded-lg border border-slate-700 p-2 text-slate-300 hover:bg-slate-800 disabled:opacity-30"
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </label>
      <div className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-slate-500">Range (trend &amp; summaries)</span>
        <div className="flex flex-wrap items-center gap-2">
          <input type="date" className={field} value={s.from ?? ""} max={s.to} onChange={(e) => e.target.value && s.go({ from: e.target.value })} />
          <span className="text-slate-500">to</span>
          <input type="date" className={field} value={s.to ?? ""} min={s.from} onChange={(e) => e.target.value && s.go({ to: e.target.value })} />
          {latest
            ? PRESETS.map((n) => (
                <button
                  key={n}
                  onClick={() => s.go({ from: addDays(latest, -n), to: latest })}
                  className="rounded-lg border border-slate-700 px-2 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
                >
                  {n === 365 ? "1y" : `${n}d`}
                </button>
              ))
            : null}
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- page body */

export default function SentimentDashboard() {
  const { selection, loading, error } = useSentimentSelection();
  const [slot, setSlot] = useState(DAY);
  if (error && isNotFound(error)) {
    return (
      <div className="card p-5 text-sm text-slate-400">
        The HQ database is not connected. Set <code className="text-slate-200">WAYSTONE_HQ_DB_HOST</code> on the API.
      </div>
    );
  }
  if (error) return <QueryGate isLoading={false} isError error={error} />;
  const strategy = selection.strategy === ALL ? undefined : selection.strategy;
  return (
    <div>
      <div className="mb-4">
        <h1 className="text-2xl font-semibold">Sentiment &amp; strategy selector</h1>
        <p className="mt-1 max-w-4xl text-sm text-slate-500">
          Text and positioning never pick direction: each layer can only permit, shrink or halt a strategy. Gates follow
          the Waystone sentiment thesis (event blackout, wire-corroborated kill switch, V221 F&amp;G × chop gate, VIX
          regime, COT crowding). Every score, gate decision and verdict is stored per session and per 30-minute interval.
        </p>
      </div>
      <FilterBar s={selection} />
      {loading ? <QueryGate isLoading isError={false} /> : null}
      {!loading && !selection.date ? (
        <div className="card p-5 text-sm text-slate-400">
          No sentiment sessions yet. Run the <code className="text-slate-200">waystone-sentiment-backfill</code> job.
        </div>
      ) : null}
      {!loading && selection.date ? (
        <>
          <NowCard strategy={strategy} />
          <DayDetail
            key={`${selection.date}-${strategy ?? ALL}`}
            date={selection.date}
            strategy={strategy}
            slot={slot}
            setSlot={setSlot}
          />
          <Trend from={selection.from} to={selection.to} onPick={(d) => selection.go({ date: d })} />
          <RangeSummaries
            from={selection.from}
            to={selection.to}
            strategy={strategy}
            current={selection.date}
            onPick={(d) => {
              setSlot(DAY);
              selection.go({ date: d });
            }}
          />
          <Quality strategy={strategy} />
        </>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------- recommender */

function gatesFor(gates: SentimentGate[], code: string) {
  const by = new Map(gates.filter((g) => g.strategy_code === code).map((g) => [g.gate, g]));
  return GATE_ORDER.map((name) => by.get(name)).filter((g): g is SentimentGate => Boolean(g));
}

function gateTitle(g: SentimentGate) {
  const parts = [
    `${GATE_LABEL[g.gate] ?? g.gate}: ${g.state}`,
    g.reason,
    g.confidence != null ? `confidence ${Math.round(g.confidence * 100)}%` : "",
    g.size_mult !== 1 ? `size ${size(g.size_mult)}` : "",
    g.inputs_as_of ? `inputs as of ${stamp(g.inputs_as_of)}` : "",
    g.expires_at ? `expires ${stamp(g.expires_at)}` : "",
    g.override ? `override: ${g.override}` : "",
    ...g.evidence.map((e) => `• ${e}`),
  ];
  return parts.filter(Boolean).join("\n");
}

function GateChips({ gates }: { gates: SentimentGate[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {gates.map((g) => (
        <span key={g.gate} title={gateTitle(g)} className={`rounded px-1.5 py-0.5 text-[11px] ${gateClass(g.state)}`}>
          {g.gate}
          {g.state === "OPEN" ? "" : ` ${g.state.toLowerCase()}`}
        </span>
      ))}
    </div>
  );
}

function RecommenderTable({ recs, gates }: { recs: SentimentRecommendation[]; gates: SentimentGate[] }) {
  if (recs.length === 0) return <div className="p-5 text-sm text-slate-500">No recommendations for this interval.</div>;
  return (
    <table className="w-full text-sm">
      <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
        <tr>
          <th className="px-5 py-2">#</th>
          <th className="px-3 py-2">Strategy</th>
          <th className="px-3 py-2">Verdict</th>
          <th className="px-3 py-2">Size</th>
          <th className="px-3 py-2" title="Shrunk mean net P&L in this regime (F&G bucket / vol state)">
            Regime fit
          </th>
          <th className="px-3 py-2">Gates (hover for evidence)</th>
          <th className="px-3 py-2">Why</th>
        </tr>
      </thead>
      <tbody>
        {recs.map((r) => (
          <tr key={r.strategy_code} className="border-t border-slate-800 align-top">
            <td className="px-5 py-2 text-slate-500">{r.rank}</td>
            <td className="px-3 py-2">
              <div className="font-medium">{r.display_name}</div>
              <div className="text-xs text-slate-500">{r.instrument_root}</div>
            </td>
            <td className="px-3 py-2">
              <Badge value={r.verdict} />
            </td>
            <td className="px-3 py-2 font-mono">{size(r.size_mult)}</td>
            <td className="px-3 py-2">
              <div className={signTone(r.fit_score)}>{r.fit_score == null ? "—" : `$${r.fit_score.toFixed(0)}`}</div>
              <div className="text-xs text-slate-500">
                {r.fit_regime ?? "—"} · n={r.fit_n}
              </div>
            </td>
            <td className="px-3 py-2">
              <GateChips gates={gatesFor(gates, r.strategy_code)} />
            </td>
            <td className="max-w-md px-3 py-2 text-xs text-slate-400">
              {r.reasons.length ? r.reasons.join("; ") : "All gates open."}
              {r.positioning ? <div className="mt-1 text-slate-500">{r.positioning}</div> : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function NowCard({ strategy }: { strategy?: string }) {
  const now = useQuery({ queryKey: ["hq-sentiment-now"], queryFn: getHqSentimentNow, refetchInterval: NOW_REFRESH_MS });
  const snap = now.data?.snapshot;
  const recs = (now.data?.recommendations ?? []).filter((r) => !strategy || r.strategy_code === strategy);
  const best = recs.find((r) => r.verdict !== "STAND_DOWN");
  return (
    <Section
      title="Recommender — latest interval"
      subtitle={
        snap
          ? `${snap.session_date} · ${snap.slot_label === DAY ? "whole session" : `${snap.slot_label} ET`} · computed ${stamp(snap.computed_at)} · policy ${snap.policy_version}`
          : "Waiting for the first sentiment run"
      }
      right={best ? <Badge value={best.verdict} title={`${best.display_name} at ${size(best.size_mult)}`} /> : null}
    >
      {now.isLoading ? <div className="p-5"><QueryGate isLoading isError={false} /></div> : null}
      {now.isError ? <div className="p-5"><QueryGate isLoading={false} isError error={now.error} /></div> : null}
      {snap ? (
        <>
          <div className="border-b border-slate-800 px-5 py-4">
            <div className="text-lg font-medium">{snap.headline}</div>
            <div className="mt-1 text-sm text-slate-400">
              {best
                ? `Pick: ${best.display_name} at ${size(best.size_mult)} size.`
                : "Pick: none — every strategy is stood down this interval."}
            </div>
          </div>
          <RecommenderTable recs={recs} gates={now.data?.gates ?? []} />
        </>
      ) : null}
    </Section>
  );
}

/* ---------------------------------------------------------------- one day */

function DayDetail({
  date,
  strategy,
  slot,
  setSlot,
}: {
  date: string;
  strategy?: string;
  slot: string;
  setSlot: (s: string) => void;
}) {
  const day = useQuery({ queryKey: ["hq-sentiment-day", date, strategy], queryFn: () => getHqSentimentDay(date, strategy) });
  if (day.isLoading || day.isError) return <QueryGate isLoading={day.isLoading} isError={day.isError} error={day.error} />;
  const data = day.data;
  const slots = data?.slots ?? [];
  const daySnap = slots.find((s) => s.slot_label === DAY);
  const intervals = slots.filter((s) => s.slot_label !== DAY).sort((a, b) => a.slot_label.localeCompare(b.slot_label));
  const active = slots.find((s) => s.slot_label === slot) ?? daySnap ?? intervals[intervals.length - 1];
  const activeLabel = active?.slot_label ?? DAY;
  const recs = (data?.recommendations ?? []).filter((r) => r.slot_label === activeLabel).sort((a, b) => a.rank - b.rank);
  const gates = (data?.gates ?? []).filter((g) => g.slot_label === activeLabel);
  const scores = (data?.scores ?? []).filter((s) => s.slot_label === activeLabel);
  return (
    <>
      {active ? <SummaryCard snap={active} events={data?.events ?? []} /> : null}
      <Section
        title={`Strategy recommender — ${activeLabel === DAY ? "whole session" : `${activeLabel} ET interval`}`}
        subtitle="Ranked: tradeable first, then regime fit. Size = product of gate multipliers, capped at 1.0× (thesis: no size-ups until Tier-0 cells are green)."
      >
        <RecommenderTable recs={recs} gates={gates} />
      </Section>
      <Timeline intervals={intervals} daySnap={daySnap} recs={data?.recommendations ?? []} active={activeLabel} onPick={setSlot} />
      <GateAudit gates={gates} />
      <Scores scores={scores} />
      <Headlines headlines={data?.headlines ?? []} />
      <Events events={data?.events ?? []} upcoming={data?.upcoming ?? []} date={date} />
    </>
  );
}

function SummaryCard({ snap, events }: { snap: SentimentSnapshot; events: SentimentEvent[] }) {
  const reg = snap.regime ?? {};
  const label = snap.slot_label === DAY ? "Session summary" : `Interval summary — ${snap.slot_label} ET`;
  const evs = snap.events?.length ? snap.events : events;
  return (
    <Section
      title={`${label} · ${snap.session_date}`}
      subtitle={`${snap.is_final ? "final" : "preliminary"} · regime ${reg.key ?? "—"} · inputs ${snap.inputs_hash ?? "—"}`}
    >
      <div className="grid gap-3 p-5 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="F&G (CNN)" value={num(snap.fng_cnn, 0)} sub={fngState(snap.fng_cnn)} tone={fngTone(snap.fng_cnn)} />
        <Stat label="F&G replica" value={num(snap.fng_replica, 0)} sub="in-house, 6 components" tone={fngTone(snap.fng_replica)} />
        <Stat
          label="Prior-day F&G (gate input)"
          value={num(snap.fng_prior_day, 0)}
          sub={`${reg.fng_source ?? ""} · gate at ≤ 30`}
          tone={fngTone(snap.fng_prior_day)}
        />
        <Stat
          label="VIX"
          value={num(snap.vix, 2)}
          sub={`VIX/VIX3M ${num(snap.vix_term_ratio, 2)} · ${reg.vol ?? "—"}${snap.vol_spike ? " · spike" : ""}`}
          tone={snap.vol_spike || (snap.vix_term_ratio ?? 0) > 1 ? "text-rose-400" : undefined}
        />
        <Stat
          label="Narrative"
          value={snap.narrative_score == null ? "—" : `${snap.narrative_score >= 0 ? "+" : ""}${snap.narrative_score.toFixed(2)}`}
          sub={`n=${snap.narrative_n ?? 0} · dispersion ${num(snap.narrative_dispersion, 2)}`}
          tone={signTone(snap.narrative_score)}
        />
        <Stat
          label="Kill-switch hits"
          value={String(snap.kill_hits ?? 0)}
          sub={evs.length ? evs.map((e) => e.kind).join(", ") : "no macro release"}
          tone={(snap.kill_hits ?? 0) > 0 ? "text-rose-400" : undefined}
        />
      </div>
      <div className="grid gap-3 px-5 pb-4 md:grid-cols-3">
        {Object.entries(reg.chop ?? {}).map(([root, chop]) => {
          const ow = reg.one_way_prior?.[root];
          return (
            <div key={root} className="rounded-lg border border-slate-800 px-3 py-2 text-xs text-slate-400">
              <span className="font-medium text-slate-200">{root}</span> · chop (20d efficiency){" "}
              <span className={chop != null && chop <= 0.03 ? "text-amber-300" : ""}>{num(chop, 3)}</span>
              {ow ? ` · prior day ${ow.direction}, body ${num(ow.body_ratio, 2)}${ow.one_way ? " (one-way)" : ""}` : ""}
            </div>
          );
        })}
      </div>
      {snap.summary ? <p className="border-t border-slate-800 px-5 py-4 text-sm leading-relaxed text-slate-300">{snap.summary}</p> : null}
      {snap.data_gaps?.length ? (
        <div className="border-t border-slate-800 px-5 py-3 text-xs text-amber-300">Data gaps: {snap.data_gaps.join(", ")}</div>
      ) : null}
    </Section>
  );
}

function Timeline({
  intervals,
  daySnap,
  recs,
  active,
  onPick,
}: {
  intervals: SentimentSnapshot[];
  daySnap?: SentimentSnapshot;
  recs: SentimentRecommendation[];
  active: string;
  onPick: (slot: string) => void;
}) {
  const rows = daySnap ? [daySnap, ...intervals] : intervals;
  const verdicts = (label: string) => recs.filter((r) => r.slot_label === label).sort((a, b) => a.rank - b.rank);
  return (
    <Section
      title="Intraday intervals"
      subtitle={
        intervals.length
          ? `${intervals.length} 30-minute intervals · click one to see its gates, scores and verdicts`
          : "No intraday intervals for this session (the intraday job runs 07:00–16:30 ET on trading days)"
      }
    >
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-5 py-2">Interval</th>
            <th className="px-3 py-2">Read</th>
            <th className="px-3 py-2">F&amp;G</th>
            <th className="px-3 py-2">VIX</th>
            <th className="px-3 py-2">Narrative</th>
            <th className="px-3 py-2">Kill</th>
            <th className="px-3 py-2">Verdicts</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr
              key={s.slot_label}
              onClick={() => onPick(s.slot_label)}
              className={`cursor-pointer border-t border-slate-800 align-top hover:bg-slate-800/40 ${
                s.slot_label === active ? "bg-emerald-600/10" : ""
              }`}
            >
              <td className="px-5 py-2 font-mono text-xs">{s.slot_label === DAY ? "Session" : s.slot_label}</td>
              <td className="max-w-md px-3 py-2 text-xs text-slate-300">{s.headline}</td>
              <td className={`px-3 py-2 ${fngTone(s.fng_cnn ?? s.fng_replica)}`}>
                {num(s.fng_cnn, 0)} <span className="text-xs text-slate-500">/ {num(s.fng_replica, 0)}</span>
              </td>
              <td className="px-3 py-2">
                {num(s.vix, 2)} <span className="text-xs text-slate-500">{num(s.vix_term_ratio, 2)}</span>
              </td>
              <td className={`px-3 py-2 ${signTone(s.narrative_score)}`}>
                {num(s.narrative_score, 2)} <span className="text-xs text-slate-500">n={s.narrative_n ?? 0}</span>
              </td>
              <td className={`px-3 py-2 ${(s.kill_hits ?? 0) > 0 ? "text-rose-400" : "text-slate-500"}`}>{s.kill_hits ?? 0}</td>
              <td className="px-3 py-2">
                <div className="flex flex-wrap gap-1">
                  {verdicts(s.slot_label).map((r) => (
                    <span key={r.strategy_code} className={`rounded px-1.5 py-0.5 text-[11px] ${gateClass(r.verdict)}`}>
                      {r.strategy_code} {size(r.size_mult)}
                    </span>
                  ))}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function GateAudit({ gates }: { gates: SentimentGate[] }) {
  const [open, setOpen] = useState(false);
  const sorted = [...gates].sort(
    (a, b) => a.strategy_code.localeCompare(b.strategy_code) || GATE_ORDER.indexOf(a.gate) - GATE_ORDER.indexOf(b.gate),
  );
  const fired = sorted.filter((g) => g.state !== "OPEN");
  const shown = open ? sorted : fired;
  return (
    <Section
      title="Gate audit trail"
      subtitle={`${fired.length} of ${gates.length} gate decisions not open · each row is stored with confidence, evidence, input timestamp, expiry and policy version`}
      right={
        <button onClick={() => setOpen(!open)} className="rounded-lg border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800">
          {open ? "Only non-open" : "Show all"}
        </button>
      }
    >
      {shown.length === 0 ? (
        <div className="p-5 text-sm text-slate-500">Every gate is open for this interval.</div>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-5 py-2">Strategy</th>
              <th className="px-3 py-2">Gate</th>
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2">Reason</th>
              <th className="px-3 py-2">Conf.</th>
              <th className="px-3 py-2">Size</th>
              <th className="px-3 py-2">Inputs as of</th>
              <th className="px-3 py-2">Expires</th>
              <th className="px-3 py-2">Evidence</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((g) => (
              <tr key={`${g.strategy_code}-${g.gate}`} className="border-t border-slate-800 align-top">
                <td className="px-5 py-2">{g.strategy_code}</td>
                <td className="px-3 py-2">{GATE_LABEL[g.gate] ?? g.gate}</td>
                <td className="px-3 py-2">
                  <Badge value={g.state} />
                  {g.override ? <div className="mt-1 text-[11px] text-sky-300">override: {g.override}</div> : null}
                </td>
                <td className="max-w-xs px-3 py-2 text-xs text-slate-300">{g.reason || "—"}</td>
                <td className="px-3 py-2 text-xs">{g.confidence == null ? "—" : `${Math.round(g.confidence * 100)}%`}</td>
                <td className="px-3 py-2 font-mono text-xs">{size(g.size_mult)}</td>
                <td className="px-3 py-2 text-xs text-slate-400">{stamp(g.inputs_as_of)}</td>
                <td className="px-3 py-2 text-xs text-slate-400">{stamp(g.expires_at)}</td>
                <td className="max-w-sm px-3 py-2 text-xs text-slate-500">{g.evidence.join(" · ") || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}

const LAYER_ORDER = ["fng", "flow", "regime", "positioning", "narrative", "efficacy", "drift"];
const LAYER_LABEL: Record<string, string> = {
  fng: "Fear & Greed",
  flow: "Volatility / flow",
  regime: "Regime",
  positioning: "Positioning (CFTC COT)",
  narrative: "Narrative (headlines)",
  efficacy: "Gate efficacy",
  drift: "Drift (PSI)",
};

function Scores({ scores }: { scores: SentimentScore[] }) {
  const layers = useMemo(() => {
    const by = new Map<string, SentimentScore[]>();
    for (const s of scores) by.set(s.layer, [...(by.get(s.layer) ?? []), s]);
    return [...by.entries()].sort((a, b) => LAYER_ORDER.indexOf(a[0]) - LAYER_ORDER.indexOf(b[0]));
  }, [scores]);
  return (
    <Section title="Every stored score" subtitle={`${scores.length} score rows for this interval, kept separate by layer (never blended)`}>
      <div className="grid gap-4 p-5 lg:grid-cols-2">
        {layers.map(([layer, rows]) => (
          <div key={layer} className="rounded-lg border border-slate-800">
            <div className="border-b border-slate-800 px-3 py-2 text-xs font-medium uppercase tracking-wide text-slate-400">
              {LAYER_LABEL[layer] ?? layer}
            </div>
            <table className="w-full text-xs">
              <tbody>
                {rows.map((r) => (
                  <tr key={r.component} className="border-t border-slate-800/60" title={r.detail ? JSON.stringify(r.detail) : undefined}>
                    <td className="px-3 py-1.5 text-slate-300">{r.component}</td>
                    <td className="px-3 py-1.5 text-right font-mono">{num(r.value, Math.abs(r.value ?? 0) < 1 ? 3 : 2)}</td>
                    <td className="px-3 py-1.5 text-right font-mono text-slate-400">
                      {r.score != null && r.score !== r.value ? num(r.score, 2) : ""}
                    </td>
                    <td className="px-3 py-1.5 text-slate-400">{r.state ?? ""}</td>
                    <td className="px-3 py-1.5 text-slate-600">{r.source ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
    </Section>
  );
}

type HeadlineFilter = "all" | "kill" | "factual" | "wire";

function Headlines({ headlines }: { headlines: SentimentHeadline[] }) {
  const [filter, setFilter] = useState<HeadlineFilter>("factual");
  const [limit, setLimit] = useState(40);
  const rows = headlines.filter((h) =>
    filter === "kill" ? h.kill_terms.length > 0 : filter === "factual" ? !h.is_speculative && h.tier > 0 : filter === "wire" ? h.tier >= 1 : true,
  );
  const counts = {
    all: headlines.length,
    factual: headlines.filter((h) => !h.is_speculative && h.tier > 0).length,
    wire: headlines.filter((h) => h.tier >= 1).length,
    kill: headlines.filter((h) => h.kill_terms.length > 0).length,
  };
  const labels: Record<HeadlineFilter, string> = { factual: "Factual", wire: "Wire only", kill: "Kill terms", all: "All" };
  return (
    <Section
      title="Headlines"
      subtitle="Scored with FinBERT (lexicon fallback). Tier: wire 1.0 · aggregator 0.5 · junk 0. Speculative / opinion items never trip the kill switch."
      right={
        <div className="flex gap-1">
          {(Object.keys(labels) as HeadlineFilter[]).map((k) => (
            <button
              key={k}
              onClick={() => setFilter(k)}
              className={`rounded-lg px-2 py-1 text-xs ${filter === k ? "bg-emerald-600/20 text-emerald-300" : "text-slate-400 hover:bg-slate-800"}`}
            >
              {labels[k]} ({counts[k]})
            </button>
          ))}
        </div>
      }
    >
      {rows.length === 0 ? (
        <div className="p-5 text-sm text-slate-500">No headlines match.</div>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-5 py-2">Time</th>
              <th className="px-3 py-2">Headline</th>
              <th className="px-3 py-2">Source</th>
              <th className="px-3 py-2">Score</th>
              <th className="px-3 py-2">Novelty</th>
              <th className="px-3 py-2">Flags</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, limit).map((h) => (
              <tr key={h.headline_id} className="border-t border-slate-800 align-top">
                <td className="whitespace-nowrap px-5 py-2 font-mono text-xs text-slate-400">{time(h.published_at)}</td>
                <td className="max-w-xl px-3 py-2">
                  <a href={h.url} target="_blank" rel="noopener noreferrer" className="hover:text-emerald-300">
                    {h.title} <ExternalLink size={11} className="inline text-slate-600" />
                  </a>
                </td>
                <td className="px-3 py-2 text-xs">
                  <div className="text-slate-300">{h.publisher ?? h.feed}</div>
                  <div className="text-slate-500">{tierLabel(h.tier)}</div>
                </td>
                <td className={`px-3 py-2 font-mono text-xs ${signTone(h.score)}`} title={h.scorer}>
                  {num(h.score, 2)}
                </td>
                <td className="px-3 py-2 font-mono text-xs text-slate-400">{num(h.novelty, 2)}</td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1 text-[11px]">
                    {h.kill_eligible ? <span className="rounded bg-rose-600/20 px-1.5 py-0.5 text-rose-300">kill-eligible</span> : null}
                    {h.kill_terms.map((t) => (
                      <span key={t} className="rounded bg-rose-900/30 px-1.5 py-0.5 text-rose-200">
                        {t}
                      </span>
                    ))}
                    {h.macro_tags.map((t) => (
                      <span key={t} className="rounded bg-sky-600/20 px-1.5 py-0.5 text-sky-200">
                        {t}
                      </span>
                    ))}
                    {h.is_speculative ? <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">speculative</span> : null}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {rows.length > limit ? (
        <button onClick={() => setLimit(limit + 60)} className="w-full border-t border-slate-800 py-2 text-xs text-slate-400 hover:bg-slate-800">
          Show more ({rows.length - limit} remaining)
        </button>
      ) : null}
    </Section>
  );
}

function Events({ events, upcoming, date }: { events: SentimentEvent[]; upcoming: SentimentEvent[]; date: string }) {
  const later = upcoming.filter((e) => (e.session_date ?? "") > date);
  return (
    <Section title="Macro calendar" subtitle="CPI / NFP / PCE / FOMC from BLS, BEA and the Fed. Intraday blackout: 30 min before to 60 min after.">
      <div className="grid gap-4 p-5 md:grid-cols-2">
        <EventList title="This session" events={events} empty="No scheduled release." />
        <EventList title="Next 14 days" events={later} empty="Nothing scheduled." />
      </div>
    </Section>
  );
}

function EventList({ title, events, empty }: { title: string; events: SentimentEvent[]; empty: string }) {
  return (
    <div>
      <div className="mb-2 text-xs uppercase tracking-wide text-slate-500">{title}</div>
      {events.length === 0 ? <div className="text-sm text-slate-500">{empty}</div> : null}
      {events.map((e) => (
        <div key={`${e.kind}-${eventTs(e)}`} className="flex gap-3 border-t border-slate-800 py-1.5 text-sm">
          <span className="w-14 rounded bg-sky-600/20 px-1.5 py-0.5 text-center text-xs text-sky-200">{e.kind}</span>
          <span className="w-36 text-xs text-slate-400">{stamp(eventTs(e))}</span>
          <span className="text-slate-300">{e.title}</span>
        </div>
      ))}
    </div>
  );
}

/* ----------------------------------------------------------------- trend */

function Trend({ from, to, onPick }: { from?: string; to?: string; onPick: (d: string) => void }) {
  const series = (layer: string, component: string) => ({
    queryKey: ["hq-sentiment-series", layer, component, from, to],
    queryFn: () => getHqSentimentSeries(layer, component, from, to),
  });
  const cnn = useQuery(series("fng", "cnn"));
  const replica = useQuery(series("fng", "replica"));
  const vix = useQuery(series("flow", "vix"));
  const narrative = useQuery(series("narrative", "all"));
  const loading = cnn.isLoading || replica.isLoading || vix.isLoading;
  return (
    <Section title="Trend" subtitle={`${from ?? "…"} to ${to ?? "…"} · click a point to open that session`}>
      {loading ? (
        <div className="p-5">
          <QueryGate isLoading isError={false} />
        </div>
      ) : (
        <div className="grid gap-4 p-5 lg:grid-cols-3">
          <LineChart
            title="Fear & Greed"
            lines={[
              { label: "CNN", color: "#34d399", points: cnn.data?.points ?? [] },
              { label: "replica", color: "#60a5fa", points: replica.data?.points ?? [] },
            ]}
            domain={[0, 100]}
            guides={[30, 70]}
            onPick={onPick}
          />
          <LineChart title="VIX" lines={[{ label: "VIX", color: "#f87171", points: vix.data?.points ?? [] }]} onPick={onPick} />
          <LineChart
            title="Narrative score"
            lines={[{ label: "narrative", color: "#fbbf24", points: narrative.data?.points ?? [] }]}
            domain={[-1, 1]}
            guides={[0]}
            onPick={onPick}
          />
        </div>
      )}
    </Section>
  );
}

function LineChart({
  title,
  lines,
  domain,
  guides = [],
  onPick,
}: {
  title: string;
  lines: { label: string; color: string; points: SentimentPoint[] }[];
  domain?: [number, number];
  guides?: number[];
  onPick: (d: string) => void;
}) {
  const W = 420;
  const H = 150;
  const P = 24;
  const dates = [...new Set(lines.flatMap((l) => l.points.map((p) => p.session_date)))].sort();
  const values = lines.flatMap((l) => l.points.map((p) => p.value).filter((v): v is number => v != null));
  if (dates.length < 2 || values.length === 0) {
    return (
      <div className="rounded-lg border border-slate-800 p-3">
        <div className="text-xs text-slate-400">{title}</div>
        <div className="py-10 text-center text-xs text-slate-600">Not enough sessions in range.</div>
      </div>
    );
  }
  const [lo, hi] = domain ?? [Math.min(...values) * 0.95, Math.max(...values) * 1.05];
  const x = (d: string) => P + (dates.indexOf(d) / (dates.length - 1)) * (W - 2 * P);
  const y = (v: number) => H - P - ((v - lo) / (hi - lo || 1)) * (H - 2 * P);
  return (
    <div className="rounded-lg border border-slate-800 p-3">
      <div className="flex justify-between text-xs text-slate-400">
        <span>{title}</span>
        <span className="flex gap-3">
          {lines.map((l) => (
            <span key={l.label} style={{ color: l.color }}>
              {l.label} {num(l.points[l.points.length - 1]?.value, 1)}
            </span>
          ))}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="mt-1 w-full">
        {guides.map((g) => (
          <g key={g}>
            <line x1={P} x2={W - P} y1={y(g)} y2={y(g)} stroke="#334155" strokeDasharray="3 3" />
            <text x={2} y={y(g) + 3} fontSize="9" fill="#64748b">
              {g}
            </text>
          </g>
        ))}
        <text x={P} y={H - 6} fontSize="9" fill="#64748b">
          {dates[0]}
        </text>
        <text x={W - P} y={H - 6} fontSize="9" fill="#64748b" textAnchor="end">
          {dates[dates.length - 1]}
        </text>
        {lines.map((l) => {
          const pts = l.points.filter((p) => p.value != null);
          return (
            <g key={l.label}>
              <polyline
                fill="none"
                stroke={l.color}
                strokeWidth="1.5"
                points={pts.map((p) => `${x(p.session_date)},${y(p.value as number)}`).join(" ")}
              />
              {pts.map((p) => (
                <circle
                  key={p.session_date}
                  cx={x(p.session_date)}
                  cy={y(p.value as number)}
                  r={pts.length > 60 ? 1.5 : 2.5}
                  fill={l.color}
                  className="cursor-pointer"
                  onClick={() => onPick(p.session_date)}
                >
                  <title>{`${p.session_date} ${l.label} ${num(p.value, 2)}${p.state ? ` (${p.state})` : ""}`}</title>
                </circle>
              ))}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/* ------------------------------------------------------------ range list */

function RangeSummaries({
  from,
  to,
  strategy,
  current,
  onPick,
}: {
  from?: string;
  to?: string;
  strategy?: string;
  current: string;
  onPick: (d: string) => void;
}) {
  const range = useQuery({ queryKey: ["hq-sentiment-range", from, to], queryFn: () => getHqSentiment(from, to) });
  const days: SentimentDaySummary[] = range.data?.days ?? [];
  const tally = useMemo(() => {
    const t: Record<Verdict, number> = { TRADE: 0, REDUCE: 0, STAND_DOWN: 0 };
    for (const d of days) for (const v of d.verdicts ?? []) if (!strategy || v.strategy_code === strategy) t[v.verdict] += 1;
    return t;
  }, [days, strategy]);
  return (
    <Section
      title="Daily sentiment summaries"
      subtitle={`${days.length} sessions · verdicts: ${tally.TRADE} trade, ${tally.REDUCE} reduce, ${tally.STAND_DOWN} stand down`}
    >
      {range.isLoading || range.isError ? (
        <div className="p-5">
          <QueryGate isLoading={range.isLoading} isError={range.isError} error={range.error} />
        </div>
      ) : null}
      <div className="divide-y divide-slate-800">
        {days.map((d) => (
          <button
            key={d.session_date}
            onClick={() => onPick(d.session_date)}
            className={`block w-full px-5 py-3 text-left hover:bg-slate-800/40 ${d.session_date === current ? "bg-emerald-600/10" : ""}`}
          >
            <div className="flex flex-wrap items-center gap-3">
              <span className="font-mono text-sm">{d.session_date}</span>
              <span className="text-sm text-slate-200">{d.headline}</span>
              <span className="ml-auto flex flex-wrap gap-1">
                {(d.verdicts ?? [])
                  .filter((v) => !strategy || v.strategy_code === strategy)
                  .map((v) => (
                    <span key={v.strategy_code} className={`rounded px-1.5 py-0.5 text-[11px] ${gateClass(v.verdict)}`}>
                      {v.rank}. {v.strategy_code} {size(v.size_mult)}
                    </span>
                  ))}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap gap-4 text-xs text-slate-500">
              <span className={fngTone(d.fng_cnn)}>F&amp;G {num(d.fng_cnn, 0)}</span>
              <span>replica {num(d.fng_replica, 0)}</span>
              <span>VIX {num(d.vix, 2)}</span>
              <span className={signTone(d.narrative_score)}>narrative {num(d.narrative_score, 2)}</span>
              {d.kill_hits ? <span className="text-rose-400">kill hits {d.kill_hits}</span> : null}
              {d.events?.length ? <span className="text-sky-300">{d.events.map((e) => e.kind).join(", ")}</span> : null}
              {d.intervals ? <span>{d.intervals} intervals</span> : null}
              {d.data_gaps?.length ? <span className="text-amber-300">gaps: {d.data_gaps.join(", ")}</span> : null}
            </div>
            {d.summary ? <div className="mt-1 line-clamp-2 text-xs text-slate-400">{d.summary}</div> : null}
          </button>
        ))}
      </div>
    </Section>
  );
}

/* --------------------------------------------------------------- quality */

function Quality({ strategy }: { strategy?: string }) {
  const q = useQuery({ queryKey: ["hq-sentiment-quality"], queryFn: getHqSentimentQuality, refetchInterval: NOW_REFRESH_MS * 5 });
  if (q.isLoading || q.isError) return <QueryGate isLoading={q.isLoading} isError={q.isError} error={q.error} />;
  const efficacy = (q.data?.efficacy ?? []).filter((r) => r.layer === "efficacy" && (!strategy || r.component.endsWith(`:${strategy}`)));
  const drift = (q.data?.efficacy ?? []).filter((r) => r.layer === "drift");
  const sources = q.data?.sources ?? [];
  const overrides = q.data?.overrides ?? [];
  const stale = (ts: string | null) => !ts || Date.now() - new Date(ts).getTime() > 36 * 3600 * 1000;
  return (
    <Section
      title="Gate quality"
      subtitle="Does each gate earn its keep? Mean net P&L on sessions it fired vs stayed open (edge = open − fired; positive means it avoided worse days). Drift alerts at PSI ≥ 0.2."
    >
      <div className="grid gap-4 p-5 lg:grid-cols-2">
        <div>
          <div className="mb-2 text-xs uppercase tracking-wide text-slate-500">Efficacy</div>
          {efficacy.length === 0 ? (
            <div className="text-sm text-slate-500">No P&amp;L history joined yet.</div>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-left text-slate-500">
                <tr>
                  <th className="py-1">Gate : strategy</th>
                  <th className="py-1 text-right">Fired avg</th>
                  <th className="py-1 text-right">Open avg</th>
                  <th className="py-1 text-right">Edge</th>
                  <th className="py-1 text-right">n fired / open</th>
                  <th className="py-1 pl-3">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {efficacy.map((r) => {
                  const d = (r.detail ?? {}) as { n_fired?: number; n_open?: number; mean_open?: number | null };
                  return (
                    <tr key={r.component} className="border-t border-slate-800">
                      <td className="py-1">{r.component}</td>
                      <td className={`py-1 text-right font-mono ${signTone(r.value)}`}>{num(r.value, 0)}</td>
                      <td className={`py-1 text-right font-mono ${signTone(d.mean_open)}`}>{num(d.mean_open, 0)}</td>
                      <td className={`py-1 text-right font-mono ${signTone(r.score)}`}>{num(r.score, 0)}</td>
                      <td className="py-1 text-right text-slate-400">
                        {d.n_fired ?? 0} / {d.n_open ?? 0}
                      </td>
                      <td className="py-1 pl-3 text-slate-400">{r.state}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          <div className="mb-2 mt-5 text-xs uppercase tracking-wide text-slate-500">Drift</div>
          {drift.map((r) => (
            <div key={r.component} className="flex justify-between border-t border-slate-800 py-1 text-xs">
              <span>{r.component}</span>
              <span className={r.state === "ALERT" ? "text-rose-400" : "text-slate-400"}>
                {r.value == null ? "not enough history" : `${r.value.toFixed(3)} · ${r.state}`}
              </span>
            </div>
          ))}
          <div className="mb-2 mt-5 text-xs uppercase tracking-wide text-slate-500">Active overrides</div>
          {overrides.length === 0 ? <div className="text-xs text-slate-500">None.</div> : null}
          {overrides.map((o) => (
            <div key={`${o.gate}-${o.valid_from}`} className="border-t border-slate-800 py-1 text-xs text-sky-200">
              {o.action} {o.gate} {o.strategy_code ?? "(all)"} — {o.reason} · by {o.created_by} · until {stamp(o.valid_to)}
            </div>
          ))}
        </div>
        <div>
          <div className="mb-2 text-xs uppercase tracking-wide text-slate-500">Source health (free feeds)</div>
          <table className="w-full text-xs">
            <tbody>
              {sources.map((s) => {
                const failing = s.last_error_at && (!s.last_ok_at || s.last_error_at > s.last_ok_at);
                return (
                  <tr key={s.source} className="border-t border-slate-800" title={s.last_error ?? undefined}>
                    <td className="py-1">{s.source}</td>
                    <td className="py-1">
                      <Badge value={failing ? "FAILING" : stale(s.last_ok_at) ? "STALE" : "OK"} />
                    </td>
                    <td className="py-1 text-slate-400">{stamp(s.last_ok_at)}</td>
                    <td className="py-1 text-right text-slate-500">{s.last_rows ?? "—"} rows</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </Section>
  );
}
