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
