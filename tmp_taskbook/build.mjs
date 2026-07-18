import fs from 'node:fs/promises';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const outputDir = 'C:/Users/Administrator/Desktop/ALIS/outputs/alis_task_breakdown';
await fs.mkdir(outputDir, { recursive: true });
const wb = Workbook.create();
const tasks = wb.worksheets.add('任务分解');
const milestones = wb.worksheets.add('里程碑');
const guide = wb.worksheets.add('字典与说明');

const rows = [
['P0','数据底座','数据源体系','TuShare数据源接入与权限校验','验证Token、接口权限、调用配额与失败重试策略','无','数据源清单、权限检查报告','核心接口可调用且失败可追踪','P0','进行中','后端/数据','1周',0.7,'受Token权限和限流影响'],
['P0','数据底座','数据源体系','行情、财务、公告、行业数据源映射','建立字段级来源、更新频率和替代来源映射','数据源接入','data_asset_contract.yaml更新','每个核心资产有来源、频率和质量规则','P0','进行中','数据工程','1周',0.6,''],
['P0','数据底座','数据仓库','主业务库与数据中台库PostgreSQL分库','完成alis_sim与alis_data连接、迁移和健康检查','基础环境','数据库迁移脚本、健康接口','生产运行不使用SQLite，双库健康检查通过','P0','已完成','后端/运维','1周',1,''],
['P0','数据底座','数据仓库','原始层/标准层/特征层表结构','建立行情、财务、公告、行业、特征的可追溯表结构','数据资产映射','DDL、索引和表说明','原始值保留、标准字段可查询、来源时间可追溯','P0','进行中','数据工程','2周',0.7,'部分事实表已存在'],
['P0','数据底座','时间一致性','available_time与point-in-time约束','禁止未来数据进入研究、回测和决策','数据仓库','时间一致性校验与测试','未来数据测试全部拦截','P0','部分实现','后端/量化','1周',0.6,'需扩大到所有特征'],
['P0','数据底座','增量更新','日行情与快照增量任务','按交易日自动更新行情、估值、涨跌停、复权等','交易日历、数据源','每日任务与运行日志','任务可重跑、幂等、失败可恢复','P0','进行中','数据工程','2周',0.7,''],
['P0','数据底座','全量补全','生成补全计划','按资产、日期、依赖和覆盖率生成任务计划','数据资产契约','补全计划接口和页面','计划与实际执行步骤一致','P0','已完成','数据工程','1周',1,''],
['P0','数据底座','全量补全','自动执行、重试与断点续跑','点击执行后持续处理可获取数据，失败进入重试/阻塞','补全计划','backfill执行器、状态接口','服务重启后可续跑，任务状态可审计','P0','部分实现','数据工程','2周',0.75,'受数据源权限和限流影响'],
['P0','数据底座','数据质量','覆盖率、时效性、重复与异常检测','建立质量规则、失败队列和质量报告','标准层表结构','质量规则与质量API','核心数据质量不达标时阻止研究决策','P0','部分实现','数据工程','2周',0.7,'长期SLO仍需验收'],
['P0','数据底座','可观测性','Redis Streams任务队列与运行日志','建立ACK、pending、retry、dead-letter和运行日志','Redis环境','redis_stream_contract.yaml、消费组','失败可重试且不丢任务','P0','基础可用','后端/运维','1周',0.85,'需真实故障注入'],
['P0','数据底座','存储演进','PostgreSQL分区与索引优化','按trade_date/available_time规划分区并压测','时间序列表','分区清单、索引报告','历史查询和补全查询满足SLO','P0','建设中','数据库','2周',0.35,''],
['P1','研究闭环','风险过滤','硬否决规则','ST、重大违法、持续亏损、流动性和财务异常过滤','财务/公告/行情数据','risk_filter接口与规则表','硬否决结果可解释、可追溯、可测试','P1','部分实现','研究工程','2周',0.65,''],
['P1','研究闭环','风险过滤','强预警与人工复核','对高负债、现金流恶化、治理风险生成预警','风险过滤','预警列表与人工确认状态','预警进入研究状态并可复核','P1','部分实现','研究工程','2周',0.55,''],
['P1','研究闭环','企业分类','企业类型与行业分类','建立稳定盈利、周期、高增长、金融等分类','股票主表、行业映射','分类字段与分类接口','分类可用于估值模型路由','P1','建设中','研究工程','2周',0.4,'真实行业映射不足'],
['P1','研究闭环','基本面研究','财务指标读取与覆盖率','读取盈利、资本效率、现金流、负债和股东回报指标','财务数据','股票研究页与覆盖率报告','指标来源、期末日和可用时间清楚','P1','部分实现','研究工程','3周',0.65,'部分字段覆盖不足'],
['P1','研究闭环','基本面研究','多维研究状态与证据快照','形成质量、成长、现金流、治理的分项状态','财务指标','research_score_snapshots','历史状态可比较且证据可下钻','P1','建设中','研究工程','3周',0.45,'历史证据快照未完整贯通'],
['P1','研究闭环','行业研究','行业指标定义与采集','建立需求、价格、库存、产能、盈利扩散等指标','行业数据源','industry_metric_definitions/values','行业指标按频率更新并带来源','P1','部分实现','行业研究','3周',0.55,''],
['P1','研究闭环','行业研究','行业状态机与公司映射','将行业景气状态映射到公司业务暴露和相对位置','行业指标、公司主表','行业状态机、公司映射表','行业状态变化可影响研究和仓位门控','P1','建设中','行业研究','4周',0.35,''],
['P1','研究闭环','公告事件','公告原文与事件分类','采集原文、来源、发布日期并分类事件类型','公告数据源','announcement_raw/event表','原文可追溯，类型分类可校验','P1','部分实现','事件研究','2周',0.65,''],
['P1','研究闭环','公告事件','影响衰减与跟进状态','建立事件有效期、影响方向、复核和关闭状态','事件分类','事件状态机与跟进页','事件不会永久污染评分，跟进可审计','P1','部分实现','事件研究','2周',0.5,''],
['P2','动态适应','市场状态','真实HSMM状态概率','基于多变量序列训练HSMM并输出状态概率','足够历史行情、流动性和波动数据','HSMM模型、状态概率接口','输出概率、状态转移和训练样本版本','P2','建设中','量化研究','4周',0.25,'历史数据补全是前置条件'],
['P2','动态适应','市场状态','市场状态历史与可视化','保存每日状态、概率和状态变化原因','市场状态模型','market-context/history','可回放任意交易日状态','P2','部分实现','后端/前端','2周',0.65,'透明状态机已有'],
['P2','动态适应','市场情绪','真实日度情绪观测','接入宽度、赚钱效应、成交、涨跌停、资金和背离指标','行情、资金、指数数据','sentiment snapshot表','每日观测持久化且缺失明确降级','P2','部分实现','量化研究','3周',0.5,'部分真实观测已接入'],
['P2','动态适应','主题叙事','自动主题发现','从公告、新闻、行业和价格扩散中发现主题','公告/新闻/行业/行情','主题发现任务与主题表','主题有来源、生命周期和置信度','P2','建设中','研究工程','4周',0.2,''],
['P2','动态适应','主题叙事','公司业务暴露与扩散图','将主题关联到公司业务、行业和扩散路径','主题发现、公司映射','主题-公司暴露证据图','每个暴露有证据，可追溯','P2','建设中','研究工程','4周',0.15,''],
['P2','动态适应','预期变化','一致预期与盈利预测版本','保存不同日期的预测、来源和版本差异','财务/分析师数据源','forecast_versions表','预测可按决策时点回放','P2','建设中','研究工程','4周',0.25,''],
['P2','动态适应','预期变化','超预期与预测修正广度','计算超预期、修正方向、修正广度和历史趋势','预测版本、财务实际值','expectation接口与快照','超预期判断有基准和数据版本','P2','建设中','量化研究','3周',0.25,''],
['P2','动态适应','策略门控','市场/情绪/主题动态仓位门控','将市场状态、情绪、组合风险和主题证据统一进入仓位门','市场状态、情绪、组合风险','position_gate与解释','门控可解释、可回放、可降级','P2','部分实现','组合/量化','2周',0.65,'正式HSMM和主题仍缺'],
['P3','投资闭环','投资逻辑','逻辑卡片与假设管理','建立逻辑、假设、必要条件、催化剂和失效条件','股票研究、事件、行业','investment_theses及页面','逻辑状态可创建、更新和审计','P3','部分实现','研究工程','2周',0.75,''],
['P3','投资闭环','投资逻辑','自动证据采集','按逻辑假设自动收集财务、公告、行业和市场证据','逻辑卡片、数据资产','证据采集任务','证据带来源、时点和可信度','P3','建设中','研究工程','4周',0.3,''],
['P3','投资闭环','投资逻辑','贝叶斯复核与自动失效','按证据更新置信度并触发定时复核/失效判定','证据采集','bayesian-review接口、复核日志','后验概率、证据权重和失效原因可解释','P3','部分实现','研究工程','3周',0.55,'框架已具备，自动化不足'],
['P3','投资闭环','估值','分类估值路由','按企业类型选择PE/PB/DCF/PS/NAV等方法','企业分类、财务数据','valuation model router','每个估值有方法和适用性说明','P3','建设中','估值研究','3周',0.35,''],
['P3','投资闭环','估值','三情景估值与安全边际','建立悲观/基准/乐观情景、价值区间和安全边际门控','分类估值、预测版本','valuation_snapshots','输入、公式和安全边际可审计','P3','建设中','估值研究','4周',0.3,''],
['P3','投资闭环','股票池','三级股票池与准入退出规则','实现观察池、核心池、持仓池及升级降级规则','研究状态、估值、逻辑','stock_pool_memberships、规则表','变更需证据和人工确认','P3','部分实现','组合/研究','3周',0.65,''],
['P3','投资闭环','组合管理','目标权重与Black-Litterman','融合先验权重、观点、置信度和风险约束','估值、逻辑、组合风险','目标权重、调整解释','每个目标权重有来源和调整原因','P3','部分实现','组合工程','3周',0.65,'透明BL-like已实现'],
['P3','投资闭环','组合管理','行业约束与再平衡说明','统一单票、行业、现金和回撤约束并生成调仓解释','目标权重、风险约束','rebalance建议与解释','约束冲突可解释，禁止越权交易','P3','部分实现','组合工程','3周',0.5,''],
['P3','投资闭环','持仓风险','逐仓投资逻辑风险','把逻辑状态、证据缺失、失效条件映射到持仓风险','投资逻辑、持仓状态','position-risk详情','逐仓风险可生成状态迁移建议','P3','部分实现','风险工程','3周',0.55,''],
['P3','投资闭环','持仓风险','压力测试与自动减仓闭环','实现市场、行业、单票压力测试及确认后的减仓流程','组合风险、成交模拟','stress tests、position-state history','建议、确认、执行、结果全链路可追踪','P3','部分实现','风险/组合','3周',0.55,'自动执行仍需谨慎验收'],
['P3','投资闭环','模拟交易','模拟成交与费用模型','支持买卖、滑点、佣金、印花税和成交账本','账户、持仓、价格','executions、positions、fees','成交后账户和持仓可对账','P3','基础可用','交易模拟','2周',0.85,''],
['P3','投资闭环','决策日志','决策证据版本冻结','冻结决策时可用数据、模型版本、研究状态和证据哈希','所有研究/模型输入','decision_snapshots证据字段','同一快照可校验、可重放','P3','部分实现','后端/治理','2周',0.65,'哈希冻结已实现'],
['P3','投资闭环','复盘','决策回放与收益归因','关联决策、成交批次、基准/行业/选股/执行归因','决策快照、成交账本','review_records、attribution表','收益归因可重算且不混淆后续价格收益','P3','部分实现','复盘工程','4周',0.45,'真实成交批次仍需贯通'],
['P4','模型治理','专业模型','因子、风险和组合模型组件','统一特征、标签、风险和优化器接口','历史数据、特征契约','模型组件与版本表','模型输入输出、版本和适用范围明确','P4','部分实现','量化研究','4周',0.45,''],
['P4','模型治理','LLM研究','LLM证据抽取与结构化校验','从公告/研报提取事件和证据，遵守schema与人工复核','原文数据、证据schema','llm_evidence_schema及抽取任务','不合规输出拒绝进入决策链','P4','建设中','研究工程','4周',0.25,''],
['P4','模型治理','Champion/Challenger','模型比较、晋升与回退','比较版本差异、holdout、walk-forward和门禁','模型版本、验证集','compare/promote/rollback接口','回退可执行且保留审计记录','P4','部分实现','模型治理','3周',0.65,''],
['P4','模型治理','SLO治理','统一SLO视图与长期验收','汇总成功率、新鲜度、P95延迟、积压和告警','任务日志、质量指标','slo/evaluate与治理页面','可按服务/时间窗口追踪SLO并触发告警','P4','部分实现','平台治理','3周',0.45,'长期样本尚未完成'],
['P4','模型治理','样本外验证','多周期统一样本外验收','统一holdout、walk-forward、不同市场阶段和组合级指标','历史行情、模型输出','oos/report、验收报告','所有生产模型按周期通过或明确失败','P4','部分实现','量化研究','4周',0.45,'历史数据补全后执行'],
['P4','模型治理','漂移与降级','数据/模型漂移监控与降级','识别特征、性能和数据覆盖漂移，自动降级到安全策略','模型运行日志、质量指标','drift、fallback、告警规则','异常时自动收缩风险并可回退','P4','部分实现','模型治理','3周',0.4,''],
['P4','模型治理','策略工作台','长期自动调优调度','按固定窗口训练、比较、晋升或回退，保留版本差异','样本外验收、模型治理','scheduler、run logs','调优不绕过门禁，不影响当前Champion','P4','部分实现','模型治理','4周',0.45,'调度器已具备基础'],
['P4','平台化','前端产品','18个研究/治理页面一致交互','统一加载、错误、空状态、权限和证据下钻体验','后端API、设计规范','前端页面与组件库','关键链路无断链，状态和限制清楚','P4','部分实现','前端','4周',0.65,''],
['P4','平台化','验收与发布','端到端验收、备份和发布流程','建立测试、数据验收、备份、回滚和发布清单','所有模块','发布Runbook、验收报告','生产发布可重复、可回退、可审计','P4','建设中','平台/运维','3周',0.3,'']
];

const headers = ['优先级','里程碑','模块','开发任务','任务说明','前置依赖','交付物','验收标准','优先级标签','状态','建议负责人','计划周期','完成度','备注'];
tasks.getRange('A1:N1').merge();
tasks.getRange('A1').values = [['ALIS 长线投资系统｜开发任务分解表']];
tasks.getRange('A2:N2').merge();
tasks.getRange('A2').values = [['来源：ALIS长线投资系统工程计划书 V1.0；当前状态结合 PLAN_IMPLEMENTATION_MATRIX 与 COMPLETION_AUDIT，完成度为工程判断值，可由项目负责人持续修订。']];
tasks.getRange('A4:N4').values = [headers];
tasks.getRange(`A5:N${rows.length+4}`).values = rows;

tasks.getRange('A1:N1').format = { fill:'#17365D', font:{bold:true,color:'#FFFFFF',size:16}, horizontalAlignment:'center', verticalAlignment:'center' };
tasks.getRange('A2:N2').format = { fill:'#D9EAF7', font:{italic:true,color:'#404040',size:10}, wrapText:true };
tasks.getRange('A4:N4').format = { fill:'#1F4E78', font:{bold:true,color:'#FFFFFF'}, horizontalAlignment:'center', verticalAlignment:'center', wrapText:true, borders:{preset:'all',style:'thin',color:'#B7C9D6'} };
tasks.getRange(`A5:N${rows.length+4}`).format = { verticalAlignment:'center', wrapText:true, borders:{preset:'inside',style:'thin',color:'#D9E2F3'} };
tasks.getRange(`M5:M${rows.length+4}`).format.numberFormat = '0%';
tasks.getRange(`A5:B${rows.length+4}`).format.horizontalAlignment = 'center';
tasks.getRange(`I5:M${rows.length+4}`).format.horizontalAlignment = 'center';
tasks.getRange(`M5:M${rows.length+4}`).conditionalFormats.add('dataBar',{color:'#5B9BD5',gradient:true});
tasks.getRange(`K5:K${rows.length+4}`).conditionalFormats.add('containsText',{text:'已完成',format:{fill:'#E2F0D9',font:{color:'#006100',bold:true}}});
tasks.getRange(`K5:K${rows.length+4}`).conditionalFormats.add('containsText',{text:'进行中',format:{fill:'#FFF2CC',font:{color:'#9C6500',bold:true}}});
tasks.getRange(`K5:K${rows.length+4}`).conditionalFormats.add('containsText',{text:'建设中',format:{fill:'#FCE4D6',font:{color:'#C65911',bold:true}}});
tasks.getRange(`K5:K${rows.length+4}`).conditionalFormats.add('containsText',{text:'部分实现',format:{fill:'#FFF2CC',font:{color:'#9C6500'}}});
tasks.freezePanes.freezeRows(4);
tasks.freezePanes.freezeColumns(4);
tasks.showGridLines = false;
const tbl = tasks.tables.add(`A4:N${rows.length+4}`, true, 'ALISTasks');
tbl.style = 'TableStyleMedium2';
tbl.showFilterButton = true;
tasks.getRange('A1:N1').format.rowHeight = 30;
tasks.getRange('A2:N2').format.rowHeight = 32;
tasks.getRange('A4:N4').format.rowHeight = 32;
const widths = [10,14,18,28,40,24,28,36,12,12,14,12,10,24];
for (let i=0;i<widths.length;i++) tasks.getRangeByIndexes(0,i,rows.length+4,1).format.columnWidth = widths[i];

milestones.getRange('A1:H1').merge();
milestones.getRange('A1').values = [['ALIS 里程碑与阶段验收']];
milestones.getRange('A3:H3').values = [['里程碑','阶段目标','计划范围','关键输出','进入条件','退出/验收条件','当前判断','说明']];
const ms = [
['M0','数据底座可用','P0','双PostgreSQL、数据源、补全、质量、Redis任务链路','数据库和TuShare可用','核心数据可补全、质量可观测、失败可恢复','进行中','历史覆盖和长期SLO尚未完全验收'],
['M1','研究闭环可用','P1','风险过滤、分类、基本面、行业、公告、多维研究状态','P0完成，核心财务覆盖达标','候选股票可形成可解释研究状态','部分实现','真实指标映射和行业状态机仍需补全'],
['M2','动态适应可用','P2','市场状态、情绪、主题、预期、动态门控','足够历史行情和基本面数据','状态概率、主题和预期变化进入决策门控','建设中','HSMM、自动主题和预测版本是关键缺口'],
['M3','持仓闭环可用','P3','逻辑、估值、股票池、组合、持仓、快照、复盘','研究状态和估值输入可用','从研究准入到成交后复盘可回放','部分实现','真实成交归因和完整再平衡解释仍需贯通'],
['M4','模型治理可用','P4','模型、LLM证据、Champion/Challenger、SLO、OOS、漂移','历史样本和生产运行窗口充分','模型可比较、可回退、可验收、可审计','部分实现','长期样本外和SLO验收尚未完成']
];
milestones.getRange('A4:H8').values = ms;
milestones.getRange('A1:H1').format = {fill:'#17365D',font:{bold:true,color:'#FFFFFF',size:16},horizontalAlignment:'center'};
milestones.getRange('A3:H3').format = {fill:'#1F4E78',font:{bold:true,color:'#FFFFFF'},wrapText:true,horizontalAlignment:'center',borders:{preset:'all',style:'thin',color:'#B7C9D6'}};
milestones.getRange('A4:H8').format = {wrapText:true,verticalAlignment:'center',borders:{preset:'inside',style:'thin',color:'#D9E2F3'}};
milestones.getRange('G4:G8').conditionalFormats.add('containsText',{text:'进行中',format:{fill:'#FFF2CC',font:{color:'#9C6500',bold:true}}});
milestones.getRange('G4:G8').conditionalFormats.add('containsText',{text:'部分实现',format:{fill:'#FFF2CC',font:{color:'#9C6500',bold:true}}});
milestones.freezePanes.freezeRows(3); milestones.showGridLines=false;
for (let i=0;i<8;i++) milestones.getRangeByIndexes(0,i,8,1).format.columnWidth = [14,22,12,32,34,38,14,30][i];

guide.getRange('A1:F1').merge(); guide.getRange('A1').values = [['使用说明、状态字典与来源']];
guide.getRange('A3:B3').values = [['字段','说明']];
guide.getRange('A4:B10').values = [
['完成度','0%~100%的工程判断值，不等同于计划书最终验收通过率'],
['已完成','代码与接口已存在且已有测试/运行证据'],
['部分实现','已有主链路，但仍缺计划书要求的完整闭环或长期验收'],
['建设中','已有基础或设计，核心实现仍在建设'],
['负责人','建议的工作角色，不代表已分配到具体个人'],
['计划周期','以连续工作周估算，需结合数据补全和资源重新排期'],
['更新方式','建议每周更新状态、完成度、负责人、实际开始/完成日期和阻塞原因']
];
guide.getRange('D3:F3').values = [['来源文件','用途','工作簿映射']];
guide.getRange('D4:F8').values = [
['docs/ALIS长线投资系统工程计划书.md','权威计划、P0-P4、M0-M4和验收定义','任务与里程碑拆解'],
['docs/PLAN_IMPLEMENTATION_MATRIX.md','当前模块实现状态与缺口','状态、完成度和备注'],
['docs/COMPLETION_AUDIT.md','完成度审计和限制说明','验收标准与风险提示'],
['docs/data_asset_contract.yaml','数据资产、频率和来源契约','P0数据任务'],
['docs/IMPLEMENTATION_CONFLICTS.md','计划与实现冲突登记','备注与治理任务']
];
guide.getRange('A1:F1').format = {fill:'#17365D',font:{bold:true,color:'#FFFFFF',size:16},horizontalAlignment:'center'};
guide.getRange('A3:B3').format = {fill:'#1F4E78',font:{bold:true,color:'#FFFFFF'}};
guide.getRange('D3:F3').format = {fill:'#1F4E78',font:{bold:true,color:'#FFFFFF'}};
guide.getRange('A4:B10').format = {wrapText:true,verticalAlignment:'center'};
guide.getRange('D4:F8').format = {wrapText:true,verticalAlignment:'center'};
guide.showGridLines=false;
for (let i=0;i<6;i++) guide.getRangeByIndexes(0,i,12,1).format.columnWidth = [18,42,4,34,42,28][i];

const check = await wb.inspect({kind:'table', range:`任务分解!A1:N15`, include:'values,formulas', tableMaxRows:15, tableMaxCols:14, maxChars:12000});
await fs.writeFile(`${outputDir}/inspect.txt`, check.ndjson, 'utf8');
const err = await wb.inspect({kind:'match', searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A', options:{useRegex:true,maxResults:100}, summary:'formula error scan'});
await fs.writeFile(`${outputDir}/errors.txt`, err.ndjson, 'utf8');
for (const [name, range] of [['任务分解','A1:N20'],['里程碑','A1:H8'],['字典与说明','A1:F10']]) {
  const png = await wb.render({sheetName:name, range, scale:1, format:'png'});
  await fs.writeFile(`${outputDir}/${name}.png`, new Uint8Array(await png.arrayBuffer()));
}
const xlsx = await SpreadsheetFile.exportXlsx(wb);
await xlsx.save(`${outputDir}/ALIS_开发任务分解表.xlsx`);
console.log(JSON.stringify({output:`${outputDir}/ALIS_开发任务分解表.xlsx`, taskCount:rows.length, inspect:check.ndjson.slice(0,500), errors:err.ndjson.slice(0,500)}));
