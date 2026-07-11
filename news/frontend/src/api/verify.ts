import axios from "axios";
import type {
  VerifyStatusResponse,
  VerifyReportResponse,
  SampleDetailResponse,
  VerifyLogsResponse,
} from "../types";

const client = axios.create({ baseURL: "/api" });

export async function fetchVerifyStatus(): Promise<VerifyStatusResponse> {
  const { data } = await client.get<VerifyStatusResponse>("/verify/status");
  return data;
}

export async function startVerify(): Promise<{ message: string }> {
  const { data } = await client.post<{ message: string }>("/verify/start");
  return data;
}

export async function stopVerify(): Promise<{ message: string }> {
  const { data } = await client.post<{ message: string }>("/verify/stop");
  return data;
}

export async function fetchVerifyReport(): Promise<VerifyReportResponse> {
  const { data } = await client.get<VerifyReportResponse>("/verify/report");
  return data;
}

export async function fetchSampleDetail(params: {
  runId?: number;
  field?: string;
  status?: string;
  page?: number;
  size?: number;
}): Promise<SampleDetailResponse> {
  const { data } = await client.get<SampleDetailResponse>("/verify/sample-detail", { params });
  return data;
}

export async function fetchVerifyLogs(limit = 300): Promise<VerifyLogsResponse> {
  const { data } = await client.get<VerifyLogsResponse>("/verify/logs", {
    params: { limit },
  });
  return data;
}
