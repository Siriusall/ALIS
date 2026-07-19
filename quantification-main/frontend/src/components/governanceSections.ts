export const GOVERNANCE_SECTION_EVENT = "alis:governance-section";
export const GOVERNANCE_SECTION_STORAGE_KEY = "alis_governance_section";

export const GOVERNANCE_SECTIONS = [
  { key: "overview", label: "治理总览", hint: "运行结论与关键状态" },
  { key: "models", label: "模型治理", hint: "训练、晋升与回退" },
  { key: "tasks", label: "任务治理", hint: "失败、暂停与恢复" },
  { key: "services", label: "数据与服务", hint: "数据质量与基础服务" },
] as const;

export type GovernanceSectionKey = (typeof GOVERNANCE_SECTIONS)[number]["key"];
