import { time } from "@/components/hq";
import type { HqBacktestTrade, HqCompare, HqMatch, HqPaperTrade } from "@/lib/types";

/** One row of the live-vs-backtest table: a matched pair, or a trade only one side took. */
export interface TradePair {
  n: number;
  live: HqPaperTrade | null;
  bt: HqBacktestTrade | null;
  match: HqMatch | null;
}

export interface DollarCheck {
  side: "Live" | "Backtest";
  label: string;
  formula: string;
  expected: number;
  actual: number;
  ok: boolean;
}

export interface DaySummary {
  tone: "good" | "bad" | "warn" | "neutral";
  headline: string;
  sentences: string[];
  warnings: string[];
  pairs: TradePair[];
  checks: DollarCheck[];
  pointValue: number | null;
}

const TOLERANCE = 1;

const money = (n: number, signed = true) =>
  `${signed && n < 0 ? "-" : ""}$${Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
const pts = (n: number) => `${n > 0 ? "+" : ""}${n.toFixed(2)} pt`;
const hhmm = (t: string | null | undefined) => (t ? t.slice(0, 5) : null);
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
const REASONS: Record<string, string> = {
  stop_loss: "the stop loss",
  take_profit: "the profit target",
  SESSION_FLATTEN: "the session flatten",
  MISSING: "missing",
  PENDING: "pending",
};
const readable = (reason: string | null | undefined) =>
  reason ? (REASONS[reason] ?? `"${reason}"`) : "unknown";
const minutesApart = (a: string | null | undefined, b: string | null | undefined) =>
  a && b ? (new Date(a).getTime() - new Date(b).getTime()) / 60000 : null;
const LOSS_LIMIT = /daily loss limit \$(-?[\d,.]+)/i;

export const capWasHit = (flag: string | boolean | null | undefined) => flag === true || flag === "Y";

/** The time the replay's session-flatten exits happened, when the run doesn't state it. */
function backtestFlattenTime(d: HqCompare): string | null {
  const flat = d.backtest_trades.filter((t) => t.exit_reason === "SESSION_FLATTEN" && t.exit_ts);
  return flat.length ? time(flat[flat.length - 1].exit_ts) : null;
}

export function buildPairs(d: HqCompare): TradePair[] {
  const byLive = new Map(d.paper_trades.map((t) => [t.entry_ts, t]));
  const byBt = new Map(d.backtest_trades.map((t) => [t.entry_ts, t]));
  if (d.matches.length > 0) {
    return d.matches.map((m, i) => ({
      n: i + 1,
      live: (m.live_entry_ts && byLive.get(m.live_entry_ts)) || null,
      bt: (m.bt_entry_ts && byBt.get(m.bt_entry_ts)) || null,
      match: m,
    }));
  }
  const live = d.paper_trades.map((t) => ({ live: t, bt: null }));
  const bt = d.paper_trades.length ? [] : d.backtest_trades.map((t) => ({ live: null, bt: t }));
  return [...live, ...bt].map((p, i) => ({ ...p, n: i + 1, match: null }));
}

function dollarChecks(d: HqCompare, pairs: TradePair[]): DollarCheck[] {
  const s = d.context.settings;
  if (!s) return [];
  const pv = d.context.backtest_run?.point_value ?? s.point_value;
  const out: DollarCheck[] = [];
  for (const p of pairs) {
    const t = p.live;
    if (t && t.points != null && t.gross_pnl != null) {
      const gross = t.points * s.point_value * t.contracts;
      out.push({
        side: "Live",
        label: `Trade ${p.n} gross`,
        formula: `${t.points.toFixed(2)} pt × $${s.point_value}/pt × ${t.contracts}`,
        expected: gross,
        actual: t.gross_pnl,
        ok: Math.abs(gross - t.gross_pnl) <= TOLERANCE,
      });
      if (t.commission != null && t.net_pnl != null) {
        const net = t.gross_pnl - t.commission;
        out.push({
          side: "Live",
          label: `Trade ${p.n} net`,
          formula: `${money(t.gross_pnl)} gross − ${money(t.commission)} commission`,
          expected: net,
          actual: t.net_pnl,
          ok: Math.abs(net - t.net_pnl) <= TOLERANCE,
        });
      }
    }
    const b = p.bt;
    if (b && b.points != null && b.contracts != null && b.net_pnl != null) {
      const net = (b.points * pv - s.commission_rt_per_contract) * b.contracts;
      out.push({
        side: "Backtest",
        label: `Trade ${p.n} net`,
        formula: `(${b.points.toFixed(2)} pt × $${pv}/pt − $${s.commission_rt_per_contract.toFixed(2)}) × ${b.contracts}`,
        expected: net,
        actual: b.net_pnl,
        ok: Math.abs(net - b.net_pnl) <= TOLERANCE,
      });
    }
  }
  const run = d.context.backtest_run;
  const counted = d.backtest_trades.filter((t) => t.net_pnl != null);
  if (run?.total_net_reported != null && counted.length) {
    const sum = counted.reduce((a, t) => a + (t.net_pnl ?? 0), 0);
    out.push({
      side: "Backtest",
      label: "Day total vs backtest report",
      formula: `sum of ${plural(counted.length, "trade")} vs the replay's own total`,
      expected: run.total_net_reported,
      actual: sum,
      ok: Math.abs(run.total_net_reported - sum) <= TOLERANCE,
    });
  }
  const paper = d.context.day_status?.checks?.paper;
  if (paper?.net_reported != null && paper.net_parsed != null) {
    // The engine's DAILY SUMMARY prints P&L before commission on most days.
    const gross = paper.net_basis === "gross" && paper.gross_parsed != null;
    const parsed = gross ? paper.gross_parsed! : paper.net_parsed;
    out.push({
      side: "Live",
      label: `Day ${gross ? "gross" : "net"} vs log DAILY SUMMARY`,
      formula: `sum of ${plural(paper.trades_parsed ?? 0, "trade")} (${gross ? "before" : "after"} commission) vs the engine's end-of-day line`,
      expected: paper.net_reported,
      actual: parsed,
      ok: paper.net_match ?? Math.abs(paper.net_reported - parsed) <= TOLERANCE,
    });
  }
  return out;
}

export function summarize(name: string, d: HqCompare): DaySummary {
  const s = d.sync;
  const ctx = d.context;
  const pairs = buildPairs(d);
  const checks = dollarChecks(d, pairs);
  const sentences: string[] = [];
  const warnings: string[] = [];
  const capBlocks = ctx.signals.filter((g) => g.outcome === "BLOCKED" && LOSS_LIMIT.test(g.block_reason ?? ""));
  const otherBlocks = ctx.signals.filter((g) => g.outcome === "BLOCKED" && !LOSS_LIMIT.test(g.block_reason ?? ""));
  const engineCap = capBlocks.length
    ? Number(LOSS_LIMIT.exec(capBlocks[0].block_reason ?? "")![1].replace(/,/g, ""))
    : null;
  const cap = engineCap ?? ctx.settings?.daily_loss_cap ?? null;
  const pointValue = ctx.settings?.point_value ?? null;

  if (!s || s.live_net_pnl == null) {
    const bt = s?.bt_net_pnl;
    return {
      tone: "neutral",
      headline: `No live paper trades were loaded for ${name} on this date.`,
      sentences:
        bt != null
          ? [`The backtest replay ${bt >= 0 ? "made" : "lost"} ${money(Math.abs(bt), false)} on ${plural(s?.bt_trades ?? 0, "trade")}.`]
          : [],
      warnings,
      pairs,
      checks,
      pointValue,
    };
  }

  const live = s.live_net_pnl;
  const liveTrades = s.live_trades ?? 0;
  const capHit = capWasHit(s.loss_cap_hit) || capBlocks.length > 0;
  let tone: DaySummary["tone"] = live > 0 ? "good" : live < 0 ? "bad" : "neutral";
  const kind =
    live > 0
      ? cap != null && live < Math.abs(cap) / 4
        ? "Small win"
        : "Win"
      : live < 0
        ? capHit
          ? "Hard loss day"
          : "Loss"
        : "Flat day";
  const headline = `${kind} for ${name}: ${plural(liveTrades, "live trade")} ${
    live >= 0 ? "made" : "lost"
  } ${money(Math.abs(live), false)}${capHit && cap != null ? `, past the ${money(cap)} daily loss cap` : ""}.`;

  const bt = s.bt_net_pnl;
  if (bt == null) {
    sentences.push(
      `There is no backtest replay for this session yet (backtest ${readable(s.backtest_status)}), so there is nothing to compare against.`,
    );
  } else {
    const gap = live - bt;
    const close = Math.abs(gap) <= Math.max(25, Math.abs(bt) * 0.02);
    sentences.push(
      close
        ? `The backtest replay ${bt >= 0 ? "made" : "lost"} ${money(Math.abs(bt), false)}, so the day totals ended up almost identical (a ${money(Math.abs(gap), false)} gap).`
        : `The backtest replay ${bt >= 0 ? "made" : "lost"} ${money(Math.abs(bt), false)} on ${plural(
            s.bt_trades ?? 0,
            "trade",
          )}, so live came in ${money(Math.abs(gap), false)} ${gap > 0 ? "better" : "worse"} than the backtest.`,
    );
  }

  const matched = pairs.filter((p) => p.match?.match_type === "MATCHED");
  if (d.matches.length > 0) {
    const reasonMismatch = matched.filter(
      (p) => p.match!.live_exit_reason && p.match!.bt_exit_reason && p.match!.live_exit_reason !== p.match!.bt_exit_reason,
    );
    if (matched.length === pairs.length && reasonMismatch.length === 0) {
      sentences.push(
        matched.length === 1
          ? `It was the same trade on both sides: same direction, ${
              Math.abs(matched[0].match!.entry_gap_s ?? 0) < 60
                ? "entry in the same minute"
                : `entries ${Math.round(Math.abs(matched[0].match!.entry_gap_s ?? 0) / 60)} min apart`
            }, and both exited on ${readable(matched[0].match!.live_exit_reason)}.`
          : `All ${matched.length} trades lined up on both sides with the same exit reasons.`,
      );
    } else {
      if (matched.length) sentences.push(`${matched.length} of ${pairs.length} trades matched between live and backtest.`);
      for (const p of reasonMismatch) {
        sentences.push(
          `Trade ${p.n} exited differently: live on ${readable(p.match!.live_exit_reason)} at ${time(p.live?.exit_ts)} ET, backtest on ${readable(p.match!.bt_exit_reason)}.`,
        );
      }
    }
    const gaps = matched
      .filter((p) => p.match!.live_points != null && p.match!.bt_points != null)
      .map((p) => ({
        n: p.n,
        gap: p.match!.live_points! - p.match!.bt_points!,
        exitGap: minutesApart(p.live?.exit_ts, p.bt?.exit_ts),
        liveExit: p.live?.exit_ts,
        btExit: p.bt?.exit_ts,
      }));
    const worst = gaps.sort((a, b) => Math.abs(b.gap) - Math.abs(a.gap))[0];
    if (worst && Math.abs(worst.gap) >= 0.25) {
      sentences.push(
        `The biggest execution gap was ${pts(worst.gap)} on trade ${worst.n}${
          pointValue ? ` (about ${money(Math.abs(worst.gap) * pointValue, false)} per contract)` : ""
        }${
          worst.exitGap != null && Math.abs(worst.exitGap) >= 2
            ? `, mostly because live exited at ${time(worst.liveExit)} ET and the backtest at ${time(worst.btExit)} ET`
            : ", which is fill slippage rather than a different signal"
        }.`,
      );
    }
    const liveOnly = pairs.filter((p) => p.match?.match_type === "PAPER_ONLY");
    if (liveOnly.length) {
      warnings.push(`${plural(liveOnly.length, "live trade")} had no backtest counterpart (trade ${liveOnly.map((p) => p.n).join(", ")}).`);
    }
    const notTaken = pairs.filter((p) => p.match?.unmatched_reason === "NOT_TAKEN_LIVE");
    if (notTaken.length) {
      warnings.push(`The backtest took ${plural(notTaken.length, "trade")} that live did not (trade ${notTaken.map((p) => p.n).join(", ")}).`);
    }
  }

  const blocked = capBlocks.reduce((a, g) => a + g.n, 0);
  const capBlocked = pairs.filter((p) => p.match?.unmatched_reason === "LOSS_CAP_BLOCKED");
  if (capHit) {
    sentences.push(
      `The daily loss cap fired live${
        blocked ? ` and blocked ${plural(blocked, "later signal")} for the rest of the session` : ""
      }${
        capBlocked.length
          ? `; the backtest took ${plural(capBlocked.length, "trade")} that live correctly skipped, so ${capBlocked.length === 1 ? "it is" : "they are"} left out of the match`
          : ""
      }. That is the risk control working as designed.`,
    );
  } else if (cap != null) {
    sentences.push(`The ${money(cap)} daily loss cap was not triggered.`);
  }
  if (otherBlocks.length) {
    const n = otherBlocks.reduce((a, g) => a + g.n, 0);
    sentences.push(
      `${plural(n, "other signal")} ${n === 1 ? "was" : "were"} filtered by the strategy's own rules (${otherBlocks
        .map((g) => g.block_reason)
        .join("; ")}).`,
    );
  }
  const refCap = ctx.settings?.daily_loss_cap;
  if (engineCap != null && refCap != null && Math.abs(engineCap - refCap) > TOLERANCE) {
    warnings.push(
      `The engine enforced a ${money(engineCap)} loss cap but the reference settings say ${money(refCap)} — update ref.strategy_settings so KPIs use the real cap.`,
    );
  }

  const liveFlat = hhmm(ctx.settings?.flatten_time);
  const btFlat = hhmm(ctx.backtest_run?.flatten_time) ?? backtestFlattenTime(d);
  if (liveFlat && btFlat && liveFlat !== btFlat) {
    tone = "warn";
    warnings.push(
      `Config mismatch: live flattens at ${liveFlat} ET but this backtest run flattened at ${btFlat} ET. Any trade still open between ${[liveFlat, btFlat].sort()[0]} and ${[liveFlat, btFlat].sort()[1]} is not an apples-to-apples comparison.`,
    );
  }
  const liveCap = cap;
  const btCap = ctx.backtest_run?.daily_loss_cap;
  if (liveCap != null && btCap != null && Math.abs(liveCap - btCap) > TOLERANCE) {
    tone = "warn";
    warnings.push(`Config mismatch: live loss cap is ${money(liveCap)} but the backtest run used ${money(btCap)}.`);
  }
  const liveFp = ctx.live_params?.params_fp;
  const btFp = ctx.backtest_run?.params_fp;
  if (liveFp && btFp && liveFp !== btFp) {
    tone = "warn";
    warnings.push(
      `Live and backtest ran different parameter sets (live ${ctx.live_params?.config_label ?? liveFp}, backtest ${ctx.backtest_run?.config_label ?? btFp}).`,
    );
  }

  const paper = ctx.day_status?.checks?.paper;
  if (paper && (paper.closed_match === false || paper.net_match === false)) {
    tone = "warn";
    warnings.push(
      `Data check failed: the trades stored for this day (${plural(paper.trades_parsed ?? 0, "trade")}, ${money(
        paper.net_parsed ?? 0,
      )}) do not agree with the engine's own DAILY SUMMARY line (${plural(paper.closed_reported ?? 0, "trade")}, ${money(
        paper.net_reported ?? 0,
      )}). Treat the live numbers for this day as unverified.`,
    );
  }

  const failed = checks.filter((c) => !c.ok);
  if (checks.length) {
    sentences.push(
      failed.length === 0
        ? "Independent dollar math (points × point value × contracts − commission) reconciles on both sides."
        : `Dollar math does not reconcile on ${failed.map((c) => `${c.side.toLowerCase()} ${c.label.toLowerCase()}`).join(", ")} — see the check table below.`,
    );
    if (failed.length) tone = "warn";
  }

  return { tone, headline, sentences, warnings, pairs, checks, pointValue };
}
