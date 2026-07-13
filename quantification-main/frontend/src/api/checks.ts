import { api } from "./client";
import type { DataHealthReport } from "../types";

export async function fetchDataHealth(limitDays = 30): Promise<DataHealthReport> {
  const { data } = await api.get<DataHealthReport>("/api/check/data-health", {
    params: { limit_days: limitDays },
  });
  return data;
}
