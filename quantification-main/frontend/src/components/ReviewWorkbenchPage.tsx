import type { SectionId } from "./Layout";

const MODULES: Array<{
  id: SectionId;
  title: string;
  description: string;
  badge: string;
}> = [
  { id: "review", title: "复盘归因", description: "记录结果并修正规则。", badge: "必做" },
  { id: "decisionLogs", title: "决策快照", description: "回看当时用了什么证据。", badge: "证据" },
  { id: "validation", title: "回测与验证", description: "检查模型结果是否可靠。", badge: "模型" },
];

interface ReviewWorkbenchPageProps {
  onNavigate?: (section: SectionId) => void;
}

export default function ReviewWorkbenchPage({ onNavigate }: ReviewWorkbenchPageProps) {
  return (
    <section className="product-module-page workbench-page">
      <div className="product-module-head">
        <div>
          <span>REVIEW HUB</span>
          <h1>人工复核</h1>
          <p>先回放，再归因，再修正。</p>
        </div>
      </div>

      <div className="workbench-hero">
        <div>
          <span>推荐路径</span>
          <strong>决策快照 → 结果归因 → 错误分类 → 规则修正</strong>
        </div>
        <button type="button" className="button primary" onClick={() => onNavigate?.("review")}>
          进入复盘归因
        </button>
      </div>

      <div className="workbench-grid workbench-grid-compact">
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
