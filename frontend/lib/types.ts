export interface Position {
  symbol: string;
  qty: number;
  avg_entry_price: number;
  market_price: number | null;
  unrealized_pnl: number | null;
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
}

export interface Order {
  symbol: string;
  side: string;
  qty: number;
  status: string;
  avg_fill_price: number | null;
  submitted_at: string;
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
