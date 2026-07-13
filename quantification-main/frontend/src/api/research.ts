export type CapabilityStatus = "ready" | "partial" | "building" | "missing";

export interface ResearchCapability {
  key: string;
  name: string;
  status: CapabilityStatus;
  api: string | null;
  missing: string[];
}

export interface ResearchCapabilitiesResponse {
  ok: boolean;
  items: ResearchCapability[];
  counts: Record<CapabilityStatus, number>;
}

export async function fetchResearchCapabilities(): Promise<ResearchCapabilitiesResponse> {
  const response = await fetch("/api/research/capabilities");
  if (!response.ok) throw new Error(`能力状态读取失败（HTTP ${response.status}）`);
  return response.json();
}

export interface StockResearchResult {
  ok: boolean;
  reason?: string;
  ts_code: string;
  profile?: { name: string; industry: string; list_date: string; trade_date: string };
  classification?: { industry: string; business_model: string; lifecycle: string };
  risk?: { eligible: boolean; risk_level: string; hard_reasons: string[]; warnings: string[]; max_position: number };
  raw_metrics?: Record<string, number | null>;
  dimensions?: Record<string, { score: number; coverage: number; confidence: number; status: string; missing: string[] }>;
  research_state?: { classification: string; confidence: number; eligible: boolean };
  evidence?: Array<{ api: string; period: string; available_time: string }>;
  limitations?: string[];
}

export async function fetchStockResearch(tsCode: string): Promise<StockResearchResult> {
  const response = await fetch(`/api/research/stocks/${encodeURIComponent(tsCode)}`);
  if (!response.ok) throw new Error(`股票研究读取失败（HTTP ${response.status}）`);
  return response.json();
}

export interface ThesisHypothesis { id: number; statement: string; metric: string; necessary: boolean; status: string; evidence: Array<Record<string, unknown>> }
export interface InvestmentThesis { id: number; ts_code: string; title: string; thesis_type: string; core_thesis: string; status: string; confidence: number; next_review_at?: string; derived_status: string; derived_confidence: number; hypotheses: ThesisHypothesis[] }

export async function fetchTheses(): Promise<InvestmentThesis[]> {
  const response = await fetch("/api/research/theses");
  if (!response.ok) throw new Error(`投资逻辑读取失败（HTTP ${response.status}）`);
  const data = await response.json();
  return data.items ?? [];
}

export async function createThesis(payload: Record<string, unknown>): Promise<InvestmentThesis> {
  const response = await fetch("/api/research/theses", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!response.ok) throw new Error(`投资逻辑创建失败（HTTP ${response.status}）`);
  return (await response.json()).item;
}

export async function updateHypothesis(id: number, status: string): Promise<InvestmentThesis> {
  const response = await fetch(`/api/research/hypotheses/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status, evidence: [{ source: "manual_review", checked_at: new Date().toISOString() }] }) });
  if (!response.ok) throw new Error(`假设状态更新失败（HTTP ${response.status}）`);
  return (await response.json()).item;
}

export interface IndustryResearchItem { industry_code: string; industry_name: string; total_evidence_points: number; metrics: Array<{ metric_code: string; metric_name: string; frequency: string; source_code: string; value_count: number; first_date?: string; last_date?: string }>; state: { score: number; coverage: number; status: string; lifecycle: string; missing: string[] }; limitations: string[] }
export async function fetchIndustries(): Promise<IndustryResearchItem[]> { const response = await fetch("/api/research/industries"); if (!response.ok) throw new Error(`行业研究读取失败（HTTP ${response.status}）`); return (await response.json()).items ?? []; }

export interface ResearchEvent { id: number; ts_code: string; event_type: string; impact_direction: string; severity: string; confidence: number; event_date: string; title: string; summary: string; evidence: { title: string; url: string; available_time: string; excerpt: string }; impact: { score: number; decayed_score: number; age_days: number; half_life_days: number } }
export async function fetchResearchEvents(tsCode = ""): Promise<ResearchEvent[]> { const query = tsCode ? `?ts_code=${encodeURIComponent(tsCode)}` : ""; const response = await fetch(`/api/research/events${query}`); if (!response.ok) throw new Error(`公告事件读取失败（HTTP ${response.status}）`); return (await response.json()).items ?? []; }

export interface LiveMarketContext { ok:boolean;trade_date:string;market_state:{state:string;confidence:number;model:string;reason:string};sentiment:{score:number;state:string;base_score:number;crowding_penalty:number;components:Record<string,number>;weights:Record<string,number>};observations:Record<string,number>;evidence:{latest_date:string;previous_date:string;symbols:number;amount_dates:number};limitations:string[];available_time:string }
export async function fetchLiveMarketContext():Promise<LiveMarketContext>{const response=await fetch("/api/model-strategy/market-context/live");if(!response.ok)throw new Error(`市场环境读取失败（HTTP ${response.status}）`);return response.json()}

export interface ExpectationState {ok:boolean;reason?:string;ts_code:string;latest_period?:string;status?:string;observations?:Record<string,number|null>;dimension?:{score:number;coverage:number;confidence:number;status:string;missing:string[]};baseline?:{target_period:string;revenue_forecast:number;profit_forecast:number;confidence:number;model_version:string}|null;limitations?:string[];evidence?:Array<{api:string;period:string;available_time:string}>}
export async function fetchExpectation(code:string):Promise<ExpectationState>{const response=await fetch(`/api/research/expectations/${encodeURIComponent(code)}`);if(!response.ok)throw new Error(`预期数据读取失败（HTTP ${response.status}）`);return response.json()}
export interface ValuationState {ok:boolean;reason?:string;ts_code:string;status?:string;method?:string;trade_date?:string;current_price?:number;current_multiple?:number;history_percentile?:number;industry_percentile?:number;scenarios?:{bear:number;base:number;bull:number};dimension?:{score:number;coverage:number;status:string;missing:string[]};evidence?:{history_points:number;peer_count:number;available_time:string};limitations?:string[]}
export async function fetchValuation(code:string):Promise<ValuationState>{const response=await fetch(`/api/research/valuations/${encodeURIComponent(code)}`);if(!response.ok)throw new Error(`估值数据读取失败（HTTP ${response.status}）`);return response.json()}
