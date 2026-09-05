import axios, { isAxiosError } from "axios";

import type {
  Account,
  ActivityEntry,
  BacktestResult,
  Bar,
  IbkrDays,
  IbkrFuturesKpis,
  IbkrOptionsKpis,
  IbkrReport,
  NewsItem,
  Order,
  Position,
  Signal,
} from "./types";

const CONFIGURED_BASE = process.env.NEXT_PUBLIC_API_BASE || "";

function resolveBase(): string {
  // Server-side (SSR / proxy target): hit FastAPI directly.
  if (typeof window === "undefined") {
    return CONFIGURED_BASE || "http://127.0.0.1:9200";
  }
  // Browser: use same-origin /api when the configured host is loopback so a
  // forwarded UI (Cursor port-forward, another machine's localhost) does not
  // hang on the viewer's own :9200.
  if (!CONFIGURED_BASE) return "";
  try {
    const host = new URL(CONFIGURED_BASE).hostname;
    if (host === "localhost" || host === "127.0.0.1") return "";
  } catch {
    return CONFIGURED_BASE;
  }
  return CONFIGURED_BASE;
}

export const TOKEN_KEY = "waystone_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}

export function apiErrorMessage(error: unknown): string {
  if (isAxiosError(error)) {
    if (error.code === "ECONNABORTED") {
      return "The API timed out. Is `waystone3 api-serve` running on port 9200?";
    }
    if (error.response?.status === 401) {
      return "Invalid username or password.";
    }
    if (!error.response) {
      return "Cannot reach the API. Start `uv run waystone3 api-serve` (the UI proxies /api to port 9200).";
    }
    const detail = (error.response.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string") return detail;
    return `API error ${error.response.status}`;
  }
  return error instanceof Error ? error.message : "Unknown error";
}

export function isNotFound(error: unknown): boolean {
  return isAxiosError(error) && error.response?.status === 404;
}

const client = axios.create({ timeout: 12_000 });
client.interceptors.request.use((cfg) => {
  cfg.baseURL = resolveBase();
  const token = getToken();
  if (token) cfg.headers.Authorization = `Bearer ${token}`;
  return cfg;
});

async function get<T>(path: string): Promise<T> {
  const { data } = await client.get<T>(path);
  return data;
}

export async function login(
  username: string,
  password: string,
): Promise<{ name: string; token: string }> {
  const { data } = await client.post<{ name: string; token: string }>("/api/login", {
    username,
    password,
  });
  setToken(data.token);
  return data;
}

export const getAccount = () => get<Account>("/api/account");
export const getPositions = () => get<Position[]>("/api/positions");
export const getOrders = () => get<Order[]>("/api/orders");
export const getActivity = () => get<ActivityEntry[]>("/api/activity");
export const getIbkrDays = () => get<IbkrDays>("/api/ibkr/days");
export const getIbkrReport = (date?: string) =>
  get<IbkrReport>(`/api/ibkr/report${date ? `?date=${date}` : ""}`);
export const getIbkrOptionsKpis = () => get<IbkrOptionsKpis>("/api/ibkr/options-kpis");
export const getIbkrFuturesKpis = () => get<IbkrFuturesKpis>("/api/ibkr/futures-kpis");
export const getSignals = (symbols = "") =>
  get<Signal[]>(`/api/signals${symbols ? `?symbols=${symbols}` : ""}`);
export const getBars = (symbol: string, lookback = 200) =>
  get<Bar[]>(`/api/bars?symbol=${symbol}&lookback=${lookback}`);
export const getNews = (symbols = "") =>
  get<NewsItem[]>(`/api/news${symbols ? `?symbols=${symbols}` : ""}`);
export const runBacktest = (params: {
  symbols: string;
  start: string;
  end: string;
  weights: string;
}) =>
  get<BacktestResult>(
    `/api/backtest?symbols=${params.symbols}&start=${params.start}&end=${params.end}&weights=${encodeURIComponent(params.weights)}`,
  );
