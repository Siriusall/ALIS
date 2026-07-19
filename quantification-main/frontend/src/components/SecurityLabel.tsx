import { useEffect, useState } from "react";
import { fetchStockNames } from "../api/positions";

const nameCache = new Map<string, string>();
const pending = new Set<string>();
const listeners = new Map<string, Set<(name: string) => void>>();
let timer: number | null = null;

function normalize(code: string) {
  return String(code || "").trim().toUpperCase();
}

function queueLookup(code: string, notify: (name: string) => void) {
  const key = normalize(code);
  if (!key || nameCache.has(key)) return () => undefined;
  const callbacks = listeners.get(key) ?? new Set<(name: string) => void>();
  callbacks.add(notify);
  listeners.set(key, callbacks);
  pending.add(key);
  if (timer == null) {
    timer = window.setTimeout(async () => {
      const batch = [...pending];
      pending.clear();
      timer = null;
      try {
        const names = await fetchStockNames(batch);
        batch.forEach((symbol) => {
          const name = names[symbol] || names[symbol.split(".")[0]] || "";
          if (name) nameCache.set(symbol, name);
          listeners.get(symbol)?.forEach((callback) => callback(name));
          listeners.delete(symbol);
        });
      } catch {
        batch.forEach((symbol) => listeners.delete(symbol));
      }
    }, 0);
  }
  return () => {
    listeners.get(key)?.delete(notify);
  };
}

export default function SecurityLabel({ code, name, className }: { code: string; name?: string | null; className?: string }) {
  const key = normalize(code);
  const supplied = String(name || "").trim();
  if (supplied) nameCache.set(key, supplied);
  const [resolved, setResolved] = useState(supplied || nameCache.get(key) || "");

  useEffect(() => {
    if (supplied) {
      setResolved(supplied);
      return undefined;
    }
    const cached = nameCache.get(key);
    if (cached) {
      setResolved(cached);
      return undefined;
    }
    return queueLookup(key, setResolved);
  }, [key, supplied]);

  return <span className={className}>{key || "—"}{resolved ? ` ${resolved}` : ""}</span>;
}
