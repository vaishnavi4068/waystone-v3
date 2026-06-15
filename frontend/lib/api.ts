import axios from "axios";

import type {
  Account,
  ActivityEntry,
  BacktestResult,
  Bar,
  NewsItem,
  Order,
  Position,
  Signal,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:9200";
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

const client = axios.create({ baseURL: BASE });
client.interceptors.request.use((cfg) => {
  const token = getToken();
  if (token) cfg.headers.Authorization = `Bearer ${token}`;
  return cfg;
});

async function get<T>(path: string): Promise<T> {
  const { data } = await client.get<T>(path);
  return data;
}

export const getAccount = () => get<Account>("/api/account");
export const getPositions = () => get<Position[]>("/api/positions");
export const getOrders = () => get<Order[]>("/api/orders");
export const getActivity = () => get<ActivityEntry[]>("/api/activity");
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
