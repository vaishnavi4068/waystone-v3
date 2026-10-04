const KEY = "hq-selection";
const EVENT = "hq-selection-change";

export const HQ_PAGES = ["/paper", "/daily", "/futures-kpis"];

export function saveSelection(query: string) {
  if (window.localStorage.getItem(KEY) === query) return;
  window.localStorage.setItem(KEY, query);
  window.dispatchEvent(new Event(EVENT));
}

export function savedSelection(): string {
  return typeof window === "undefined" ? "" : (window.localStorage.getItem(KEY) ?? "");
}

export function onSelectionChange(fn: () => void) {
  window.addEventListener(EVENT, fn);
  return () => window.removeEventListener(EVENT, fn);
}
