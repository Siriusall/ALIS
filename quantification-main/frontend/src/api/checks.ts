import axios from "axios";
import type { DataHealthReport } from "../types";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
});

export async function fetchDataHealth(limitDays = 30): Promise<DataHealthReport> {
  const { data } = await api.get<DataHealthReport>("/api/check/data-health", {
    params: { limit_days: limitDays },
  });
  return data;
}
