import axios from "axios";
import type {
  CrawlCoverageResponse,
  CrawlErrorsResponse,
  CrawlLogsResponse,
  CrawlStatusResponse,
  CrawlTriggerResponse,
} from "../types";

const client = axios.create({ baseURL: "/api" });

export async function fetchCrawlStatus(): Promise<CrawlStatusResponse> {
  const { data } = await client.get<CrawlStatusResponse>("/crawl/status");
  return data;
}

export async function fetchCrawlCoverage(runId?: number): Promise<CrawlCoverageResponse> {
  const { data } = await client.get<CrawlCoverageResponse>("/crawl/coverage", {
    params: runId ? { runId } : undefined,
  });
  return data;
}

export async function fetchCrawlErrors(runId?: number): Promise<CrawlErrorsResponse> {
  const { data } = await client.get<CrawlErrorsResponse>("/crawl/errors", {
    params: runId ? { runId } : undefined,
  });
  return data;
}

export async function fetchCrawlLogs(limit = 300): Promise<CrawlLogsResponse> {
  const { data } = await client.get<CrawlLogsResponse>("/crawl/logs", {
    params: { limit },
  });
  return data;
}

export async function triggerCrawl(): Promise<CrawlTriggerResponse> {
  const { data } = await client.post<CrawlTriggerResponse>("/crawl/trigger", {});
  return data;
}

export async function stopCrawl(): Promise<void> {
  await client.post("/crawl/stop");
}
