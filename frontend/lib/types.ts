export interface Position {
  symbol: string;
  qty: number;
  avg_entry_price: number;
  market_price: number | null;
  unrealized_pnl: number | null;
  local_symbol?: string;
  sec_type?: string;
  expiry?: string | null;
  strike?: number | null;
  right?: string | null;
  book?: string;
  exchange?: string;
  multiplier?: string | null;
  market_value?: number | null;
}

export interface Strategy {
  weights: Record<string, number>;
  watchlist: string[];
  bullish_threshold: number;
  bearish_threshold: number;
  notional_per_trade: number;
}

export interface Account {
  you: string;
  team: string[];
  broker: string;
  is_paper: boolean;
  trading_enabled: boolean;
  cash: number;
  equity: number;
  buying_power: number;
  strategy: Strategy | null;
  nlv?: number;
  excess_liquidity?: number;
  maint_margin?: number;
  currency?: string;
  report_date?: string | null;
  as_of?: string | null;
  published?: boolean;
  today_published?: boolean;
  staged?: boolean;
  staged_week?: string | null;
}

export interface Order {
  symbol: string;
  side: string;
  qty: number;
  status: string;
  avg_fill_price: number | null;
  submitted_at: string;
  local_symbol?: string;
  sec_type?: string;
  expiry?: string | null;
  strike?: number | null;
  right?: string | null;
  book?: string;
  commission?: number | null;
  realized_pnl?: number | null;
}

export interface ActivityEntry {
  seq: number;
  actor: string;
  action: string;
  detail: string;
}

export interface Signal {
  symbol: string;
  score: number;
  per_contributor: Record<string, number>;
  drivers: string[];
}

export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface BacktestResult {
  metrics: {
    total_return_pct: number;
    max_drawdown_pct: number;
    win_rate_pct: number;
    trades: number;
  };
  equity: number[];
}

export interface NewsItem {
  title: string;
  source: string;
  url: string;
  symbols: string[];
  published_at: string;
}

export interface BookStats {
  fills: number;
  qty: number;
  notional: number;
  commission: number;
  realized_pnl: number;
}

export interface IbkrExecution {
  exec_id: string;
  time: string;
  account: string;
  sec_type: string;
  symbol: string;
  local_symbol: string;
  exchange: string;
  expiry: string | null;
  strike: number | null;
  right: string | null;
  multiplier: string | null;
  side: string;
  qty: number;
  price: number;
  commission: number | null;
  realized_pnl: number | null;
  client_id: number | null;
  book: string;
}

export interface IbkrDays {
  days: string[];
  latest: string | null;
  today: string;
  today_published: boolean;
  staged?: boolean;
  staged_week?: string | null;
  staged_days?: string[];
}

export interface IbkrReport {
  date: string;
  generated_at: string;
  published: boolean;
  today: string;
  today_published: boolean;
  executions: IbkrExecution[];
  positions: Position[];
  account: {
    account_id: string;
    nlv: number;
    cash: number;
    buying_power: number;
    excess_liquidity: number;
    maint_margin: number;
    currency: string;
    equity: number;
  };
  summary: {
    date: string;
    futures: BookStats;
    options: BookStats;
    other: BookStats;
    totals: BookStats;
  };
  staged?: boolean;
  staged_week?: string | null;
}

export interface OptionsKpiRow {
  key: string;
  label: string;
  source: string;
  critical: boolean;
  target: number;
  min: number;
  direction: "ge" | "le";
  value: number | null;
  status: string;
  definition: string;
}

export interface OptionsKpiStage {
  id: string;
  name: string;
  verdict: string;
  filled: number;
  total: number;
  kpis: OptionsKpiRow[];
}

export interface IbkrOptionsKpis {
  as_of: string | null;
  days: number;
  assumptions: {
    nav: number;
    contracts_per_trade: number;
    option_multiplier: number;
    round_trip_slippage: number;
  };
  overall: string;
  stages: OptionsKpiStage[];
  weeks: { week: string; return_pct: number }[];
  trade_count: number;
  span_days: number;
  staged?: boolean;
  staged_week?: string | null;
  staged_iso_week?: string | null;
}

export interface IbkrFuturesKpis {
  as_of: string | null;
  days: number;
  instrument: string;
  assumptions: {
    nav: number;
    contracts_per_trade: number;
    point_value: number;
  };
  overall: string;
  stages: OptionsKpiStage[];
  weeks: { week: string; return_pct: number }[];
  trade_count: number;
  span_days: number;
  staged?: boolean;
  staged_week?: string | null;
  staged_iso_week?: string | null;
}

export interface AlgoConfig {
  id: string;
  name: string;
  book: string;
  live_prefix: string;
  replay_prefix: string;
  client_id: number | null;
  enabled: boolean;
  notes: string;
}

export interface AlgoList {
  algos: AlgoConfig[];
}

export interface CompareDays {
  days: string[];
  latest: string | null;
  staged?: boolean;
  staged_week?: string | null;
}

export interface CompareRow {
  status: "matched" | "live_only" | "replay_only" | string;
  symbol: string;
  local_symbol: string;
  side: string;
  qty: number;
  book: string;
  live_price: number | null;
  replay_price: number | null;
  price_delta: number | null;
  live_pnl: number | null;
  replay_pnl: number | null;
  pnl_delta: number | null;
  live_time: string | null;
  replay_time: string | null;
}

export interface AlgoCompare {
  algo: AlgoConfig;
  date: string;
  live_source: string;
  replay_source: string;
  live: BookStats;
  replay: BookStats;
  deltas: BookStats;
  matched: number;
  live_only: number;
  replay_only: number;
  avg_price_delta: number | null;
  avg_pnl_delta: number | null;
  rows: CompareRow[];
  live_fills: IbkrExecution[];
  replay_fills: IbkrExecution[];
  staged?: boolean;
  staged_week?: string | null;
}

export interface ResearchStats {
  days?: number;
  years?: number;
  total_return_pct?: number | null;
  cagr_pct?: number | null;
  ann_vol_pct?: number | null;
  sharpe?: number | null;
  sortino?: number | null;
  max_drawdown_pct?: number | null;
  calmar?: number | null;
  trade_count?: number | null;
  win_rate_pct?: number | null;
}

export interface ResearchRun {
  date: string;
  variant: string;
  run_id?: string | null;
  synthetic: boolean;
  params: Record<string, unknown>;
  stats: ResearchStats;
  extra: Record<string, unknown>;
  equity: number[];
}

export interface ResearchScorecardKpi {
  id: string;
  name: string;
  definition: string;
  unit: string;
  type: string;
  pass?: number | null;
  warn?: number | null;
  critical: boolean;
  value: number | boolean | string | null;
  status: string;
}

export interface ResearchScorecardStage {
  id: string;
  name: string;
  desc?: string;
  verdict: string;
  filled: number;
  total: number;
  kpis?: ResearchScorecardKpi[];
}

export interface ResearchScorecard {
  strategy_id?: string;
  name?: string;
  book?: string;
  variant?: string;
  date?: string;
  overall: string;
  window?: { start?: string | null; end?: string | null };
  banner?: Record<string, number | string | null | undefined>;
  notes?: string[];
  stages: ResearchScorecardStage[];
  rule_sketch?: string;
  instruments?: string;
  holding_period?: string;
  pnl_by_year?: { year: string; trades: number; pnl_usd: number }[];
  pnl_by_month?: { month: string; pnl_usd: number }[];
  trade_details?: ResearchTradeDetail[];
  trade_count_total?: number;
  trade_details_truncated?: boolean;
  avg_monthly_net_usd?: number | null;
}

export interface ResearchTradeDetail {
  entry_time?: string;
  exit_time?: string;
  symbol?: string;
  side?: string;
  qty?: number | null;
  entry_price?: number | null;
  exit_price?: number | null;
  pnl?: number;
  hold_days?: number | null;
}

export interface ResearchStrategy {
  id: string;
  name: string;
  book: string;
  instruments: string;
  holding_period: string;
  summary: string;
  rule_sketch: string;
  data_sources: string[];
  modes: string[];
  days: string[];
  variants?: string[];
  latest: ResearchRun | null;
  scorecard?: ResearchScorecard | null;
}

export interface ResearchOpsStatus {
  event?: string;
  phase?: string;
  title?: string;
  body?: string;
  approval?: string | null;
  at?: string;
  source?: string;
}

export interface ResearchOpsInboxItem {
  id: string;
  at: string;
  text: string;
  action: string;
  acked: boolean;
  source: string;
}

export interface ResearchOps {
  status: ResearchOpsStatus | null;
  inbox: ResearchOpsInboxItem[];
  writable: boolean;
}

export interface AlgoOnboard {
  id: string;
  name: string;
  book: string;
  live_prefix?: string;
  replay_prefix?: string;
  client_id?: number | null;
  enabled?: boolean;
  notes?: string;
}

export type HqStatus = "GREEN" | "AMBER" | "RED" | "NA" | "INFO" | string;

export interface HqStrategy {
  strategy_code: string;
  display_name: string;
  asset_class: string;
  instrument_root: string | null;
  paper_start_date: string | null;
  is_active: boolean;
  notes: string | null;
  as_of_date: string | null;
  trading_days: number | null;
  trades: number | null;
  net_pnl: number | null;
  return_pct: number | null;
  equity_end: number | null;
  red_count: number | null;
  amber_count: number | null;
  green_count: number | null;
  overall_gate: string | null;
  last_session: string | null;
  paper_status: string | null;
  backtest_status: string | null;
  sync_status: string | null;
  kpi_dates?: string[];
}

export interface HqScorecard {
  kpi_window: string;
  window_start: string | null;
  window_end: string | null;
  trading_days: number | null;
  trades: number | null;
  net_pnl: number | null;
  return_pct: number | null;
  equity_end: number | null;
  red_count: number;
  amber_count: number;
  green_count: number;
  overall_gate: string | null;
}

export interface HqKpi {
  kpi_window: string;
  section: string;
  section_name: string;
  sort_order: number;
  kpi_code: string;
  label: string;
  description: string | null;
  direction: string | null;
  green_at: number | null;
  amber_at: number | null;
  unit: string | null;
  num_value: number | null;
  text_value: string | null;
  status: HqStatus | null;
}

export interface HqKpis {
  strategy_code: string;
  as_of_date: string | null;
  scorecard: HqScorecard[];
  kpis: HqKpi[];
}

export interface HqSyncRow {
  strategy_code: string;
  display_name: string;
  session_date: string;
  instrument_symbol: string | null;
  live_trades: number | null;
  live_win_rate: number | null;
  live_points: number | null;
  live_net_pnl: number | null;
  live_exit_reason: string | null;
  bt_trades: number | null;
  bt_points: number | null;
  bt_net_pnl: number | null;
  bt_exit_reason: string | null;
  pnl_delta: number | null;
  pnl_delta_pct: number | null;
  exit_reason_match: boolean | null;
  exit_time_gap_min: number | null;
  loss_cap_hit: boolean | null;
  sync_status: string | null;
  notes_auto: string | null;
  notes_manual: string | null;
  paper_status: string | null;
  backtest_status: string | null;
}

export interface HqDailyPnl {
  session_date: string;
  trades: number;
  gross_pnl: number | null;
  commission: number | null;
  slippage_cost: number | null;
  net_pnl: number | null;
  equity_start: number | null;
  equity_end: number | null;
  daily_return: number | null;
  dd_itd: number | null;
}

export interface HqPaperTrade {
  session_date: string;
  trade_no: number;
  instrument: string | null;
  direction: string;
  contracts: number;
  entry_ts: string | null;
  exit_ts: string | null;
  entry_px: number | null;
  exit_px: number | null;
  entry_slip_pts: number | null;
  exit_slip_pts: number | null;
  exit_reason: string | null;
  points: number | null;
  gross_pnl: number | null;
  commission: number | null;
  slippage_cost: number | null;
  net_pnl: number | null;
  hold_min: number | null;
  mae_pts: number | null;
  mfe_pts: number | null;
}

export interface HqMatch {
  match_seq: number;
  match_type: string;
  unmatched_reason: string | null;
  live_entry_ts: string | null;
  bt_entry_ts: string | null;
  live_exit_ts: string | null;
  bt_exit_ts: string | null;
  live_exit_reason: string | null;
  bt_exit_reason: string | null;
  live_points: number | null;
  bt_points: number | null;
  live_net_pnl: number | null;
  bt_net_pnl: number | null;
  entry_gap_s: number | null;
  pnl_delta: number | null;
}

export interface HqBacktestTrade {
  trade_seq: number;
  direction: string;
  entry_ts: string | null;
  exit_ts: string | null;
  entry_px: number | null;
  exit_px: number | null;
  points: number | null;
  contracts: number | null;
  contracts_inferred: boolean;
  net_pnl: number | null;
  net_pnl_derived: boolean;
  exit_reason: string | null;
  config_label: string | null;
  params_fp: string | null;
}

export interface HqPaperChecks {
  trades_parsed?: number;
  closed_reported?: number;
  closed_match?: boolean;
  net_parsed?: number;
  net_reported?: number;
  net_match?: boolean;
  summary_present?: boolean;
  unparsed_lines?: number;
}

export interface HqDayContext {
  settings: {
    point_value: number;
    default_contracts: number;
    commission_rt_per_contract: number;
    model_slip_rt_per_contract: number;
    flatten_time: string | null;
    daily_loss_cap: number | null;
  } | null;
  backtest_run: {
    config_label: string | null;
    params_fp: string | null;
    point_value: number | null;
    flatten_time: string | null;
    daily_loss_cap: number | null;
    trades_reported: number | null;
    total_net_reported: number | null;
    status: string;
  } | null;
  live_params: { params_fp: string; config_label: string | null } | null;
  signals: { outcome: string; block_reason: string | null; n: number }[];
  day_status: {
    paper_status: string;
    backtest_status: string;
    sync_status: string;
    checks: { paper?: HqPaperChecks; sync_notes?: string[] } | null;
  } | null;
}

export interface HqCompare {
  strategy_code: string;
  session_date: string;
  sync: HqSyncRow | null;
  matches: HqMatch[];
  paper_trades: HqPaperTrade[];
  backtest_trades: HqBacktestTrade[];
  context: HqDayContext;
}

export interface HqWeek {
  week_no: number;
  week_start: string;
  week_end: string;
  trades: number;
  net_pnl: number;
  return_pct: number | null;
  max_dd: number | null;
  account_value: number | null;
}

export interface HqMonth {
  month_no: number;
  month_start: string;
  trades: number;
  net_pnl: number;
  return_pct: number | null;
  max_dd: number | null;
  sharpe: number | null;
  account_value: number | null;
}

export interface HqReturns {
  weekly: HqWeek[];
  monthly: HqMonth[];
  sync_rolling: {
    as_of_date: string;
    days_logged: number;
    all_time_live_pnl: number | null;
    all_time_bt_pnl: number | null;
    all_time_flags: number;
    flag_rate: number | null;
    last7_live_pnl: number | null;
  } | null;
}

export interface HqLoadRun {
  job: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  files_seen: number;
  files_loaded: number;
  error: string | null;
}
