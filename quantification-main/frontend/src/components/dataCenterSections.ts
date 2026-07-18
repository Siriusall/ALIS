import type { SectionKey } from "./DataCenterTechnicalConsole";

export const DATA_CENTER_SECTIONS: Array<{ key: SectionKey; label: string; hint: string }> = [
  { key: "overview", label: "总览", hint: "状态与问题" },
  { key: "readiness", label: "长线分析自检", hint: "功能数据契约" },
  { key: "assets", label: "数据资产", hint: "数据集目录" },
  { key: "archive", label: "归档与恢复", hint: "本地冷存储" },
  { key: "repair", label: "数据补全", hint: "更新与修复" },
  { key: "quality", label: "质量监控", hint: "完整度与异常" },
  { key: "sources", label: "数据源与接口", hint: "来源与健康" },
  { key: "logs", label: "运行日志", hint: "任务与审计" },
];

export const DATA_CENTER_SECTION_STORAGE_KEY = "alis:data-center-section";
export const DATA_CENTER_SECTION_EVENT = "alis:data-center-section-change";
