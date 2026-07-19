import { apiFetch } from "./client";

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
  const response = await apiFetch("/api/research/capabilities");
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
  const response = await apiFetch(`/api/research/stocks/${encodeURIComponent(tsCode)}`);
  if (!response.ok) throw new Error(`股票研究读取失败（HTTP ${response.status}）`);
  return response.json();
}

export interface ThesisHypothesis { id: number; statement: string; metric: string; necessary: boolean; status: string; evidence: Array<Record<string, unknown>> }
export interface InvestmentThesis { id: number; ts_code: string; title: string; thesis_type: string; core_thesis: string; status: string; confidence: number; next_review_at?: string; derived_status: string; derived_confidence: number; hypotheses: ThesisHypothesis[] }

export async function fetchTheses(): Promise<InvestmentThesis[]> {
  const response = await apiFetch("/api/research/theses");
  if (!response.ok) throw new Error(`投资逻辑读取失败（HTTP ${response.status}）`);
  const data = await response.json();
  return data.items ?? [];
}

export async function createThesis(payload: Record<string, unknown>): Promise<InvestmentThesis> {
  const response = await apiFetch("/api/research/theses", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!response.ok) throw new Error(`投资逻辑创建失败（HTTP ${response.status}）`);
  return (await response.json()).item;
}

export async function updateHypothesis(id: number, status: string): Promise<InvestmentThesis> {
  const response = await apiFetch(`/api/research/hypotheses/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status, evidence: [{ source: "manual_review", checked_at: new Date().toISOString() }] }) });
  if (!response.ok) throw new Error(`假设状态更新失败（HTTP ${response.status}）`);
  return (await response.json()).item;
}
export async function reviewThesis(id:number):Promise<InvestmentThesis>{const response=await apiFetch(`/api/research/theses/${id}/review`,{method:"POST"});if(!response.ok)throw new Error(`投资逻辑自动复核失败（HTTP ${response.status}）`);return (await response.json()).item}
export async function reviewDueTheses():Promise<{due:number;reviewed:Array<{thesis_id:number}>;failed:Array<{thesis_id:number;error:string}>}>{const response=await apiFetch("/api/research/theses/review-due",{method:"POST"});if(!response.ok)throw new Error(`到期投资逻辑复核失败（HTTP ${response.status}）`);return response.json()}

export interface IndustryResearchItem { industry_code: string; industry_name: string; total_evidence_points: number; company_count: number; companies: Array<{ts_code:string;name:string}>; metrics: Array<{ metric_code: string; metric_name: string; frequency: string; source_code: string; value_count: number; first_date?: string; last_date?: string }>; state: { score: number; coverage: number; status: string; lifecycle: string; missing: string[] }; state_as_of_date?: string; state_model_version?: string; limitations: string[] }
export async function fetchIndustries(asOfTime = ""): Promise<IndustryResearchItem[]> { const query = asOfTime ? `?as_of_time=${encodeURIComponent(asOfTime)}` : ""; const response = await apiFetch(`/api/research/industries${query}`); if (!response.ok) throw new Error(`行业研究读取失败（HTTP ${response.status}）`); return (await response.json()).items ?? []; }

export interface ResearchEvent { id: number; ts_code: string; event_type: string; impact_direction: string; severity: string; confidence: number; event_date: string; title: string; summary: string; evidence: { title: string; url: string; available_time: string; excerpt: string }; impact: { score: number; decayed_score: number; age_days: number; half_life_days: number };thesis_link:null|{thesis_id:number;title:string;status:string};follow_up:{status:string;next_action:string;requires_thesis_review:boolean} }
export async function reviewLinkedEventTheses(): Promise<{candidates:number;reviewed:Array<{thesis_id:number}>;failed:Array<{thesis_id:number;error:string}>}> { const response = await apiFetch("/api/research/events/review-linked?limit=100", { method: "POST" }); if (!response.ok) throw new Error(`关联逻辑复核失败（HTTP ${response.status}）`); return response.json(); }
export async function fetchResearchEvents(tsCode = ""): Promise<ResearchEvent[]> { const query = tsCode ? `?ts_code=${encodeURIComponent(tsCode)}` : ""; const response = await apiFetch(`/api/research/events${query}`); if (!response.ok) throw new Error(`公告事件读取失败（HTTP ${response.status}）`); return (await response.json()).items ?? []; }

export interface MarketProfile {code:string;label:string;parent_state:string;parent_label:string;confidence:number;risk_budget_multiplier:number;risk_ceiling_policy:string;dimensions:string[];method:string;profile_count:number}
export interface LiveMarketContext { ok:boolean;trade_date:string;market_state:{state:string;confidence:number;model:string;reason:string};market_profile?:MarketProfile;sentiment:{score:number;state:string;base_score:number;crowding_penalty:number;components:Record<string,number>;weights:Record<string,number>};observations:Record<string,number>;evidence:{latest_date:string;previous_date:string;symbols:number;amount_dates:number};limitations:string[];available_time:string }
export async function fetchLiveMarketContext():Promise<LiveMarketContext>{const response=await apiFetch("/api/model-strategy/market-context/live");if(!response.ok)throw new Error(`市场环境读取失败（HTTP ${response.status}）`);const data=await response.json();if(data?.ok===false)throw new Error(data.reason||"市场环境数据不足");return data}
export interface MarketContextGovernance { production_model:string;hsmm:{status:string;history_years:number;breadth_history_years:number;minimum_years:number;state_count?:number;profile_state_count?:number;profile_model?:string;production_eligible:boolean;blocking_reason:string;live_observation_weeks:number;minimum_live_observation_weeks:number;validation?:{status?:string};review?:{status?:string}} }
export async function fetchMarketContextGovernance():Promise<MarketContextGovernance>{const response=await apiFetch("/api/model-strategy/market-context/governance");if(!response.ok)throw new Error(`市场状态治理读取失败（HTTP ${response.status}）`);return response.json()}
export interface MarketContextHistory {ok:boolean;count:number;items:Array<{trade_date:string;market_state:string;state_confidence:number;sentiment_score:number;model_version:string;available_time:string;observations:Record<string,number>;market_profile?:MarketProfile}>;transitions:Array<{trade_date:string;from:string;to:string;confidence:number}>}
export async function fetchMarketContextHistory():Promise<MarketContextHistory>{const response=await apiFetch("/api/model-strategy/market-context/history?limit=120");if(!response.ok)throw new Error(`市场环境历史读取失败（HTTP ${response.status}）`);return response.json()}

export interface ExpectationState {ok:boolean;reason?:string;ts_code:string;latest_period?:string;status?:string;observations?:Record<string,number|null>;dimension?:{score:number;coverage:number;confidence:number;status:string;missing:string[]};baseline?:{target_period:string;revenue_forecast:number;profit_forecast:number;confidence:number;model_version:string}|null;external_forecast?:{kind:string;status:string;latest?:{period:string;ann_date:string;p_change_min?:number|null;p_change_max?:number|null;change_mid?:number|null};history?:Array<{period:string;ann_date:string;p_change_min?:number|null;p_change_max?:number|null;change_mid?:number|null}>};limitations?:string[];evidence?:Array<{api:string;period:string;available_time:string}>}
export async function fetchExpectation(code:string):Promise<ExpectationState>{const response=await apiFetch(`/api/research/expectations/${encodeURIComponent(code)}`);if(!response.ok)throw new Error(`预期数据读取失败（HTTP ${response.status}）`);return response.json()}
export interface ValuationState {ok:boolean;reason?:string;ts_code:string;status?:string;method?:string;trade_date?:string;current_price?:number;current_multiple?:number;history_percentile?:number;industry_percentile?:number;scenarios?:{bear:number;base:number;bull:number};dimension?:{score:number;coverage:number;status:string;missing:string[]};evidence?:{history_points:number;peer_count:number;available_time:string};limitations?:string[]}
export async function fetchValuation(code:string):Promise<ValuationState>{const response=await apiFetch(`/api/research/valuations/${encodeURIComponent(code)}`);if(!response.ok)throw new Error(`估值数据读取失败（HTTP ${response.status}）`);return response.json()}

export interface PortfolioRiskOverview {ok:boolean;status:string;as_of:string;account:{cash:number;market_value:number;equity:number;high_water_mark:number;drawdown:number};tail_risk:{confidence:number;var:number;cvar:number;observations:number};optimizer:string;target_weights:Record<string,number>;weight_explanations:Array<{symbol:string;current_weight:number;target_weight:number;delta:number;reason:string}>;industry_exposures:Array<{industry:string;weight:number;limit:number;status:string}>;exposures:Array<{symbol:string;name:string;industry?:string;market_value:number;weight:number;observations:number;thesis_status:string;thesis_confidence:number}>;breaches:Array<{code:string;symbol?:string;industry?:string;severity:string;message:string}>;stress_tests:Array<{code:string;name:string;pnl:number;equity_after:number;loss_pct:number;assumption:string;severity:string}>;deleveraging_actions:Array<{action:string;symbol?:string;industry?:string;priority:string;target_weight?:number;risk_budget_multiplier?:number;reason:string}>;position_state_recommendations:Array<{symbol:string;current_state:string;recommended_state:string;reason:string;evidence:{thesis_status:string;thesis_confidence:number;risk_breaches:string[]};requires_confirmation:boolean}>;limitations:string[]}
export async function fetchPortfolioRisk():Promise<PortfolioRiskOverview>{const response=await apiFetch("/api/research/portfolio-risk/live");if(!response.ok)throw new Error(`持仓风险读取失败（HTTP ${response.status}）`);return response.json()}
export async function confirmPositionState(payload:{ts_code:string;to_state:string;reason:string;evidence:Record<string,unknown>[];confirmed:true}):Promise<Record<string,unknown>>{const response=await apiFetch("/api/research/position-state/confirm",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});if(!response.ok)throw new Error(`持仓状态确认失败（HTTP ${response.status}）`);return response.json()}

export interface GovernanceOverview {ok:boolean;gates:Array<{key:string;status:string;detail:string}>;capabilities:Record<string,number>;model:{active_version?:{version?:string};latest_train_run?:{id?:number;status?:string;phase?:string;progress?:number;message?:string};version_count:number};tasks:{recent:Array<Record<string,unknown>>;failed_or_paused:Array<Record<string,unknown>>};data:{health:{ok:boolean;issues?:Array<unknown>};readiness:{consumers?:Array<{key:string;name:string;status:string}>};failed_runs:Array<Record<string,unknown>>}}
export async function fetchGovernanceOverview():Promise<GovernanceOverview>{const response=await apiFetch("/api/research/governance/overview");if(!response.ok)throw new Error(`治理总览读取失败（HTTP ${response.status}）`);return response.json()}

export interface DecisionAuditOverview {ok:boolean;counts:{decisions:number;reviews:number;awaiting_review:number};quality:{future_data_violations:number;missing_evidence:number};decisions:Array<Record<string,unknown>>;reviews:Array<Record<string,unknown>>}
export async function fetchDecisionAudit():Promise<DecisionAuditOverview>{const response=await apiFetch("/api/research/decision-audit");if(!response.ok)throw new Error(`决策审计读取失败（HTTP ${response.status}）`);return response.json()}

export interface DecisionSnapshot {
  id:number; ts_code:string; decision_type:string; decision_time:string; price?:number|null;
  data_as_of_time:string; model_version:string; reason:string; invalidation_conditions?:string[];
  market_state?:Record<string,unknown>; research_state?:Record<string,unknown>; valuation?:Record<string,unknown>; portfolio_context?:Record<string,unknown>;
}
export interface DecisionEvidenceVerification { ok:boolean; id:number; verified:boolean; status:string; schema_version:string; reason?:string; expected_hash?:string; actual_hash?:string }
export interface DecisionEvidenceDiff { ok:boolean; left_id:number; right_id:number; changed:boolean; change_count:number; changes:Array<{path:string;left:unknown;right:unknown}>; truncated:boolean }
export interface ReviewRecord { id:number; decision_snapshot_id?:number|null; review_time:string; outcome:Record<string,unknown>; attribution:Record<string,unknown>; error_type?:string|null; lessons:string; created_at:string }
export async function fetchDecisionSnapshots(limit=100):Promise<DecisionSnapshot[]>{const response=await apiFetch(`/api/research/decisions?limit=${limit}`);if(!response.ok)throw new Error(`决策快照读取失败（HTTP ${response.status}）`);return ((await response.json()).items??[]) as DecisionSnapshot[]}
export async function verifyDecisionEvidence(id:number):Promise<DecisionEvidenceVerification>{const response=await apiFetch(`/api/research/evidence/verify/${id}`);if(!response.ok)throw new Error(`决策证据校验失败（HTTP ${response.status}）`);return response.json()}
export async function diffDecisionEvidence(leftId:number,rightId:number):Promise<DecisionEvidenceDiff>{const response=await apiFetch(`/api/research/evidence/diff?left_id=${leftId}&right_id=${rightId}`);if(!response.ok)throw new Error(`决策快照对比失败（HTTP ${response.status}）`);return response.json()}
export async function fetchReviews(limit=100):Promise<ReviewRecord[]>{const response=await apiFetch(`/api/research/reviews?limit=${limit}`);if(!response.ok)throw new Error(`复盘记录读取失败（HTTP ${response.status}）`);return ((await response.json()).items??[]) as ReviewRecord[]}
export async function createReview(payload:{decision_snapshot_id?:number;outcome:Record<string,unknown>;attribution:Record<string,unknown>;error_type?:string;lessons:string}):Promise<ReviewRecord>{const response=await apiFetch("/api/research/reviews",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});if(!response.ok)throw new Error(`保存复盘失败（HTTP ${response.status}）`);return (await response.json()).item as ReviewRecord}
export async function fetchReviewSuggestion(id:number):Promise<{ok:boolean;reason?:string;outcome?:{directional_return?:number;note?:string};attribution?:{method?:string;limitations?:string[]}}>{const response=await apiFetch(`/api/research/reviews/suggestion/${id}`);if(!response.ok)throw new Error(`复盘结果计算失败（HTTP ${response.status}）`);return response.json()}

export interface LlmReviewDraft {ok:boolean;model:string;decision_snapshot_id:number;draft:{summary:string;outcome_assessment:string;error_type:string;attribution:string;lessons:string;evidence_points:string[];confidence:number;limitations:string[]};disclaimer:string}
export async function generateLlmReviewDraft(id:number):Promise<LlmReviewDraft>{const response=await apiFetch(`/api/research/reviews/llm-draft/${id}`,{method:"POST"});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data?.detail === "deepseek_api_key_not_configured" ? "请先在 .env 填写 DEEPSEEK_API_KEY 并重载后端" : `LLM复核草稿生成失败（HTTP ${response.status}）`);return data}

export interface ManualReviewQueueItem {key:string;kind:"decision_review"|"event_evidence"|"model_release"|"system_exception"|"data_quality";priority:"high"|"normal"|"low";title:string;reason:string;status:string;created_at?:string;subject:string;evidence:Record<string,unknown>;target_section:string}
export interface ManualReviewQueue {ok:boolean;counts:{total:number;high:number;decision_review:number;event_evidence:number;model_release:number;system_exception:number;data_quality:number};items:ManualReviewQueueItem[];ai_triage:{pending:number;auto_resolved:number;escalated:number;awaiting_outcome:number;running:boolean};llm_budget:{date:string;used_tokens:number;daily_budget:number;remaining_tokens:number;per_request_limit:number;requests:number;status:"available"|"warning"|"exhausted"}}
export async function fetchManualReviewQueue():Promise<ManualReviewQueue>{const response=await apiFetch("/api/research/manual-review/queue");if(!response.ok)throw new Error(`人工复核队列读取失败（HTTP ${response.status}）`);return response.json()}
export interface ReviewLearningOverview {ok:boolean;counts:{outcomes:number;decisions:number;feedback_samples:number;models:number};accuracy:number|null;error_statistics:Array<{error_type:string;count:number}>;latest_outcomes:Array<{id:number;decision_snapshot_id:number;horizon_sessions:number;ts_code:string;decision_type:string;model_version:string;directional_return:number;benchmark_return:number|null;industry_return:number|null;market_contribution:number|null;industry_contribution:number|null;selection_contribution:number|null;correct:number}>}
export async function fetchReviewLearningOverview():Promise<ReviewLearningOverview>{const response=await apiFetch("/api/research/reviews/learning-overview");if(!response.ok)throw new Error(`复盘学习闭环读取失败（HTTP ${response.status}）`);return response.json()}
export async function materializeReviewOutcomes():Promise<void>{const response=await apiFetch("/api/research/reviews/materialize-outcomes",{method:"POST"});if(!response.ok)throw new Error(`收益归因更新失败（HTTP ${response.status}）`)}
export async function submitManualReviewAction(payload:{review_key:string;item_kind:string;action:"approved"|"rejected"|"modified_approved";error_type?:string;comment?:string;llm_draft?:Record<string,unknown>}):Promise<void>{const response=await apiFetch("/api/research/manual-review/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data?.detail === "rejection_reason_required" ? "驳回时请选择错误类型；选择‘其他’时还需要填写原因" : `复核结果保存失败（HTTP ${response.status}）`)}

export interface StockPoolMember {id:number;ts_code:string;pool_type:string;reason:string;status:string;entered_at:string;updated_at:string}
export interface PoolEligibility {ok:boolean;ts_code:string;eligible_for_core:boolean;gates:Array<{key:string;label:string;passed:boolean;detail:string}>;research_state?:Record<string,unknown>;valuation?:{status?:string;method?:string;scenarios?:Record<string,number>};thesis_count:number}
export async function fetchPoolEligibility(tsCode:string):Promise<PoolEligibility>{const response=await apiFetch(`/api/research/pools/eligibility/${encodeURIComponent(tsCode)}`);if(!response.ok)throw new Error(`股票池门控读取失败（HTTP ${response.status}）`);return response.json()}
export async function fetchStockPools():Promise<StockPoolMember[]>{const response=await apiFetch("/api/research/pools");if(!response.ok)throw new Error(`股票池读取失败（HTTP ${response.status}）`);return (await response.json()).items??[]}
export async function stageCandidateWatchPool(limit=20):Promise<{staged:number}>{const response=await apiFetch(`/api/research/pools/stage-candidates?limit=${limit}`,{method:"POST"});if(!response.ok)throw new Error(`候选入池失败（HTTP ${response.status}）`);return response.json()}
export async function saveStockPoolMember(ts_code:string,pool_type:string,reason:string):Promise<StockPoolMember>{const response=await apiFetch("/api/research/pools",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ts_code,pool_type,reason})});if(!response.ok)throw new Error(`股票池更新失败（HTTP ${response.status}）`);return (await response.json()).item}
export async function removeStockPoolMember(id:number):Promise<void>{const response=await apiFetch(`/api/research/pools/${id}`,{method:"DELETE"});if(!response.ok)throw new Error(`股票池移除失败（HTTP ${response.status}）`)}
