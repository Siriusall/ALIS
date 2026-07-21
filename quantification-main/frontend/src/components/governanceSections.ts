export const GOVERNANCE_SECTION_EVENT = "alis:governance-section";
export const GOVERNANCE_SECTION_STORAGE_KEY = "alis_governance_section";

export const GOVERNANCE_SECTIONS = [
  { key: "modelStatus", label: "模型状态", hint: "生产、候选、可训练" },
  { key: "modelTraining", label: "模型训练过程", hint: "训练进度与队列" },
  { key: "modelParameters", label: "模型参数", hint: "目标、标签、特征" },
  { key: "modelAutoOptimization", label: "模型自优化", hint: "自动训练，人工闸门" },
  { key: "modelPromotion", label: "模型晋升", hint: "候选批准与驳回" },
  { key: "modelVersions", label: "模型版本管理", hint: "生产版本与回退" },
  { key: "tasks", label: "任务治理", hint: "失败、暂停与恢复" },
  { key: "services", label: "数据与服务", hint: "数据质量与基础服务" },
] as const;

export type GovernanceSectionKey = (typeof GOVERNANCE_SECTIONS)[number]["key"];

export const DEFAULT_GOVERNANCE_SECTION: GovernanceSectionKey = "modelStatus";

export function normalizeGovernanceSection(value: string | null | undefined): GovernanceSectionKey {
  const key = value === "models" || value === "overview" ? DEFAULT_GOVERNANCE_SECTION : value;
  return GOVERNANCE_SECTIONS.some((item) => item.key === key)
    ? key as GovernanceSectionKey
    : DEFAULT_GOVERNANCE_SECTION;
}
