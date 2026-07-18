import type { SectionId } from "./Layout";

const MODULES: Array<{
  id: SectionId;
  title: string;
  description: string;
  badge: string;
}> = [
  { id: "stockResearch", title: "单股研究工作台", description: "集中看一只股票。", badge: "高频" },
  { id: "industryResearch", title: "行业景气", description: "先判断行业方向。", badge: "景气" },
  { id: "events", title: "公告事件", description: "追踪高影响变化。", badge: "事件" },
  { id: "sentiment", title: "市场情绪", description: "看节奏与仓位环境。", badge: "环境" },
  { id: "themes", title: "主题叙事", description: "验证主线是否成立。", badge: "主题" },
  { id: "expectation", title: "预期变化", description: "看经营是否偏离预期。", badge: "预期" },
  { id: "valuation", title: "估值与安全边际", description: "判断价格是否合适。", badge: "估值" },
];

interface ResearchWorkbenchPageProps {
  onNavigate?: (section: SectionId) => void;
}

export default function ResearchWorkbenchPage({ onNavigate }: ResearchWorkbenchPageProps) {
  return (
    <section className="product-module-page workbench-page">
      <div className="product-module-head">
        <div>
          <span>RESEARCH HUB</span>
          <h1>研究中心</h1>
          <p>选研究对象，然后进入对应工作台。</p>
        </div>
      </div>

      <div className="workbench-hero">
        <div>
          <span>推荐路径</span>
          <strong>单股研究 → 投资逻辑 → 估值安全边际 → 今日调仓候选</strong>
        </div>
        <button type="button" className="button primary" onClick={() => onNavigate?.("stockResearch")}>
          直接进入单股研究
        </button>
      </div>

      <div className="workbench-grid">
        {MODULES.map((item) => (
          <button key={item.id} type="button" className="workbench-card workbench-card-link" onClick={() => onNavigate?.(item.id)}>
            <div className="workbench-card-top">
              <span>{item.badge}</span>
              <h2>{item.title}</h2>
            </div>
            <p>{item.description}</p>
            <span className="workbench-card-enter">进入工作台 <b>→</b></span>
          </button>
        ))}
      </div>
    </section>
  );
}
