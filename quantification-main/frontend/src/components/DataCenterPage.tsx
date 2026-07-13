import { useState } from "react";
import DataCenterTechnicalConsole, { type SectionKey } from "./DataCenterTechnicalConsole";
import "./DataCenterTechnicalConsole.css";

const SECTIONS: Array<{ key: SectionKey; label: string; hint: string }> = [
  { key: "overview", label: "总览", hint: "状态与问题" },
  { key: "readiness", label: "长线分析自检", hint: "功能数据契约" },
  { key: "assets", label: "数据资产", hint: "数据集目录" },
  { key: "repair", label: "数据补全", hint: "更新与修复" },
  { key: "quality", label: "质量监控", hint: "完整度与异常" },
  { key: "sources", label: "数据源与接口", hint: "来源与健康" },
  { key: "logs", label: "运行日志", hint: "任务与审计" },
];

export default function DataCenterPage() {
  const [section, setSection] = useState<SectionKey>("overview");
  return (
    <div className="embedded-data-platform">
      <nav className="data-platform-tabs" aria-label="数据中台功能导航">
        {SECTIONS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={section === item.key ? "active" : ""}
            onClick={() => setSection(item.key)}
          >
            <strong>{item.label}</strong>
            <small>{item.hint}</small>
          </button>
        ))}
      </nav>
      <DataCenterTechnicalConsole section={section} />
    </div>
  );
}
