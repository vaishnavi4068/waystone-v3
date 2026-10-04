const KEY = "hq-selection";

export const HQ_PAGES = ["/paper", "/daily", "/futures-kpis"];

export function saveSelection(query: string) {
  window.localStorage.setItem(KEY, query);
}

export function savedSelection(): string {
  return typeof window === "undefined" ? "" : (window.localStorage.getItem(KEY) ?? "");
}
