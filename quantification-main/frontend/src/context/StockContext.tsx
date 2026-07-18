import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

const STORAGE_KEY = "alis_active_stock_v1";

interface StockContextValue {
  activeStock: string;
  setActiveStock: (code: string) => void;
}

const StockContext = createContext<StockContextValue | null>(null);

function normalizeStockCode(value: string) {
  return value.trim().toUpperCase();
}

export function StockProvider({ children }: { children: ReactNode }) {
  const [activeStock, setActiveStockState] = useState(() => normalizeStockCode(window.localStorage.getItem(STORAGE_KEY) ?? ""));
  const value = useMemo<StockContextValue>(() => ({
    activeStock,
    setActiveStock(code) {
      const normalized = normalizeStockCode(code);
      setActiveStockState(normalized);
      if (normalized) window.localStorage.setItem(STORAGE_KEY, normalized);
      else window.localStorage.removeItem(STORAGE_KEY);
    },
  }), [activeStock]);
  return <StockContext.Provider value={value}>{children}</StockContext.Provider>;
}

export function useStockContext() {
  const value = useContext(StockContext);
  if (!value) throw new Error("useStockContext must be used inside StockProvider");
  return value;
}
