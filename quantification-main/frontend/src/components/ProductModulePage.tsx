import { useEffect, useState } from "react";
import { fetchResearchCapabilities, type ResearchCapability } from "../api/research";

const COPY: Record<string, { eyebrow: string; description: string; outputs: string[] }> = {
  stock_research: { eyebrow: "COMPANY RESEARCH", description: "按行业模板形成企业分类、风险门控和多维研究状态。", outputs: ["企业画像与商业模式", "财务质量与成长", "风险、证据和失效项"] },
  industry: { eyebrow: "INDUSTRY RESEARCH", description: "跟踪需求、价格、库存、产能和盈利扩散，识别行业生命周期。", outputs: ["行业状态概率", "景气驱动证据", "公司相对位置"] },
  events: { eyebrow: "EVENT ENGINE", description: "把公告转成可验证、可跟进、会衰减的结构化事件。", outputs: ["事件方向与强度", "原文证据", "投资逻辑影响"] },
  sentiment: { eyebrow: "MARKET SENTIMENT", description: "情绪只影响节奏和风险预算，不篡改企业基本面事实。", outputs: ["赚钱效应", "风险偏好", "拥挤惩罚"] },
  themes: { eyebrow: "NARRATIVE ENGINE", description: "发现市场正在讲的故事，并验证扩散、持续性和真实业务暴露。", outputs: ["主题生命周期", "公司关联证据", "催化与拥挤"] },
  expectation: { eyebrow: "EXPECTATION CHANGE", description: "比较预测版本、实际结果和行业确认，识别预期上调与下调。", outputs: ["预测修正", "业绩惊喜", "1/2/4季度趋势"] },
  valuation: { eyebrow: "VALUATION", description: "分离公司质量与买入价格，输出悲观、基准和乐观情景。", outputs: ["历史与行业分位", "情景价值", "安全边际"] },
  pool: { eyebrow: "STOCK UNIVERSE", description: "管理研究观察池、核心候选池和实际持仓池。", outputs: ["准入门控", "候选原因", "淘汰证据"] },
  thesis: { eyebrow: "INVESTMENT THESIS", description: "由系统维护假设、必要条件、催化剂、失效条件和复核时间。", outputs: ["核心假设", "证据更新", "逻辑状态机"] },
  risk: { eyebrow: "POSITION RISK", description: "同时检查组合尾部风险和每只持仓的投资逻辑风险。", outputs: ["回撤与CVaR", "集中度", "必要条件预警"] },
  review: { eyebrow: "DECISION REVIEW", description: "冻结当时看到的证据，复盘收益来源和错误类型。", outputs: ["决策回放", "收益归因", "模型与人工改进"] },
  governance: { eyebrow: "SYSTEM GOVERNANCE", description: "统一查看任务、数据质量、SLO、模型漂移和降级状态。", outputs: ["任务与失败队列", "数据质量", "Champion/Challenger"] },
};

const STATUS = { ready: "可用", partial: "部分实现", building: "建设中", missing: "尚未实现" } as const;

export default function ProductModulePage({ capabilityKey }: { capabilityKey: string }) {
  const [capability, setCapability] = useState<ResearchCapability | null>(null);
  const [error, setError] = useState("");
  const copy = COPY[capabilityKey] ?? { eyebrow: "ALIS MODULE", description: "按计划书建设中的产品模块。", outputs: [] };
  useEffect(() => {
    let active = true;
    fetchResearchCapabilities().then(data => {
      if (active) setCapability(data.items.find(item => item.key === capabilityKey) ?? null);
    }).catch(reason => active && setError(reason instanceof Error ? reason.message : String(reason)));
    return () => { active = false; };
  }, [capabilityKey]);
  return <section className="product-module-page">
    <div className="product-module-head"><div><span>{copy.eyebrow}</span><h1>{capability?.name ?? "读取模块状态"}</h1><p>{copy.description}</p></div><span className={`module-state module-state-${capability?.status ?? "building"}`}>{capability ? STATUS[capability.status] : "检查中"}</span></div>
    {error && <div className="alert">{error}</div>}
    <div className="product-module-grid">
      <article className="product-module-card"><h2>计划输出</h2>{copy.outputs.map(item => <div className="module-output" key={item}><i />{item}</div>)}</article>
      <article className="product-module-card"><h2>当前后端能力</h2><p>{capability?.api ? `已登记接口：${capability.api}` : "尚无正式事实接口，不能生成伪结果。"}</p></article>
      <article className="product-module-card"><h2>待完成</h2>{(capability?.missing ?? ["正在读取能力清单"]).map(item => <div className="module-missing" key={item}>{item}</div>)}</article>
    </div>
  </section>;
}
