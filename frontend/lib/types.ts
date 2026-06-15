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

export interface Me {
  player: string;
  rank: number | null;
  cycles_run: number;
  account: { cash: number; equity: number; return_pct: number };
  positions: Position[];
  strategy: Strategy | null;
}

export interface Standing {
  rank: number;
  player: string;
  equity: number;
  return_pct: number;
  cycles_run: number;
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
