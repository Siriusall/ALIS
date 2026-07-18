import fs from 'node:fs/promises';
import { FileBlob, SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const srcPath = 'C:/Users/Administrator/Desktop/ALIS/outputs/alis_task_breakdown/ALIS_开发任务分解表.xlsx';
const outputDir = 'C:/Users/Administrator/Desktop/ALIS/outputs/alis_task_breakdown';
const source = await FileBlob.load(srcPath);
const oldWb = await SpreadsheetFile.importXlsx(source);
const oldSheet = oldWb.worksheets.getItem('任务分解');
const parentRows = oldSheet.getRange('A5:N54').values;
const wb = Workbook.create();
const tasks = wb.worksheets.add('任务分解');
const msSheet = wb.worksheets.add('里程碑');
const guide = wb.worksheets.add('字典与说明');

const headers = ['WBS编号','父任务','优先级','里程碑','模块','子任务类型','开发任务','任务说明','前置依赖','交付物','验收标准','状态','建议负责人','计划周期','备注'];
const types = [
  ['需求与设计','明确字段、流程、边界和异常规则，形成可评审设计','产品/计划书','设计说明、接口草案','需求评审通过，边界和异常场景齐全','产品/架构'],
  ['后端与数据','实现数据库、服务、接口、任务调度或模型逻辑','需求与设计','代码、迁移、接口','接口契约、权限、幂等和错误处理通过','后端/数据'],
  ['前端与交互','实现页面、表格、筛选、状态提示和证据下钻','后端接口','页面、组件、交互状态','加载、空状态、错误状态和核心流程可用','前端'],
  ['测试与验收','编写单元、接口、集成和数据验收用例','后端与前端实现','测试用例、验收记录','关键路径通过，失败可定位，限制已披露','测试/治理'],
];
const detail = [];
parentRows.forEach((r, i) => {
  const parentId = `WBS-${String(i + 1).padStart(3,'0')}`;
  types.forEach((t, j) => {
    const originalStatus = String(r[9] ?? '');
    const status = originalStatus.includes('完成') && j < 3 ? '已完成' : originalStatus.includes('基础') && j === 3 ? '已测试' : originalStatus.includes('进行') && j === 0 ? '进行中' : '待开发';
    detail.push([`${parentId}.${j + 1}`, parentId, r[0], r[1], r[2], t[0], `${r[3]}｜${t[0]}`, `${t[1]}；本子任务聚焦：${r[4]}`, `${t[2]}；${r[5] || '无'}`, `${t[3]}；对应工作包交付物：${r[6]}`, `${t[4]}；对应工作包验收：${r[7]}`, status, t[5], r[11], r[13] || '按工作包统一验收']);
  });
});

tasks.getRange('A1:O1').merge(); tasks.getRange('A1').values = [['ALIS 长线投资系统｜完整开发任务分解表']];
tasks.getRange('A2:O2').merge(); tasks.getRange('A2').values = [['已将计划书的 50 个工作包拆为 200 条明细任务；状态仅使用：待开发、进行中、已测试、已完成。']];
tasks.getRange('A4:O4').values = [headers]; tasks.getRange(`A5:O${detail.length + 4}`).values = detail;
tasks.getRange('A1:O1').format = {fill:'#17365D',font:{bold:true,color:'#FFFFFF',size:16},horizontalAlignment:'center'};
tasks.getRange('A2:O2').format = {fill:'#D9EAF7',font:{italic:true,color:'#404040'},wrapText:true};
tasks.getRange('A4:O4').format = {fill:'#1F4E78',font:{bold:true,color:'#FFFFFF'},horizontalAlignment:'center',wrapText:true,borders:{preset:'all',style:'thin',color:'#B7C9D6'}};
tasks.getRange(`A5:O${detail.length + 4}`).format = {wrapText:true,verticalAlignment:'center',borders:{preset:'inside',style:'thin',color:'#D9E2F3'}};
tasks.getRange(`A5:F${detail.length + 4}`).format.horizontalAlignment = 'center'; tasks.getRange(`I5:N${detail.length + 4}`).format.horizontalAlignment = 'center';
for (const [text, fill, color] of [['待开发','#F2F2F2','#666666'],['进行中','#FFF2CC','#9C6500'],['已测试','#DDEBF7','#1F4E78'],['已完成','#E2F0D9','#006100']]) tasks.getRange(`L5:L${detail.length + 4}`).conditionalFormats.add('containsText',{text,format:{fill,font:{color,bold:true}}});
tasks.freezePanes.freezeRows(4); tasks.freezePanes.freezeColumns(6); tasks.showGridLines = false;
const table = tasks.tables.add(`A4:O${detail.length + 4}`, true, 'ALISDetailedTasks'); table.style = 'TableStyleMedium2'; table.showFilterButton = true;
const widths = [12,12,10,14,18,16,30,44,24,30,38,12,14,12,24]; for (let i=0;i<widths.length;i++) tasks.getRangeByIndexes(0,i,detail.length + 4,1).format.columnWidth = widths[i];

const msValues = oldWb.worksheets.getItem('里程碑').getRange('A1:H8').values; msSheet.getRange('A1:H8').values = msValues;
msSheet.getRange('A1:H1').merge(); msSheet.getRange('A1:H1').format = {fill:'#17365D',font:{bold:true,color:'#FFFFFF',size:16},horizontalAlignment:'center'}; msSheet.getRange('A3:H3').format = {fill:'#1F4E78',font:{bold:true,color:'#FFFFFF'},wrapText:true,horizontalAlignment:'center'}; msSheet.getRange('A4:H8').format = {wrapText:true,verticalAlignment:'center'}; msSheet.showGridLines = false;
for (let i=0;i<8;i++) msSheet.getRangeByIndexes(0,i,8,1).format.columnWidth = [14,22,12,32,34,38,14,30][i];
guide.getRange('A1:F1').merge(); guide.getRange('A1').values = [['使用说明、状态字典与来源']]; guide.getRange('A3:B3').values = [['字段','说明']]; guide.getRange('A4:B10').values = [['状态','仅使用：待开发、进行中、已测试、已完成'],['WBS编号','父任务.子任务编号，例如 WBS-001.2'],['父任务','对应计划书拆出的工作包编号'],['子任务类型','需求与设计、后端与数据、前端与交互、测试与验收'],['建议负责人','角色建议，不代表已分配到具体个人'],['计划周期','以连续工作周估算，需结合资源和数据补全进度调整'],['更新方式','建议每周更新状态、负责人、实际开始/完成日期和阻塞原因']]; guide.getRange('D3:F3').values = [['来源文件','用途','工作簿映射']]; guide.getRange('D4:F8').values = [['docs/ALIS长线投资系统工程计划书.md','权威计划、P0-P4、M0-M4和验收定义','任务与里程碑拆解'],['docs/PLAN_IMPLEMENTATION_MATRIX.md','当前模块实现状态与缺口','初始状态和备注'],['docs/COMPLETION_AUDIT.md','完成度审计和限制说明','验收标准与风险提示'],['docs/data_asset_contract.yaml','数据资产、频率和来源契约','P0数据任务'],['docs/IMPLEMENTATION_CONFLICTS.md','计划与实现冲突登记','备注与治理任务']]; guide.getRange('A1:F1').format = {fill:'#17365D',font:{bold:true,color:'#FFFFFF',size:16},horizontalAlignment:'center'}; guide.getRange('A3:B3').format = {fill:'#1F4E78',font:{bold:true,color:'#FFFFFF'}}; guide.getRange('D3:F3').format = {fill:'#1F4E78',font:{bold:true,color:'#FFFFFF'}}; guide.getRange('A4:B10').format = {wrapText:true}; guide.getRange('D4:F8').format = {wrapText:true}; guide.showGridLines = false;
for (let i=0;i<6;i++) guide.getRangeByIndexes(0,i,10,1).format.columnWidth = [18,42,4,34,42,28][i];

const check = await wb.inspect({kind:'table',range:'任务分解!A1:O15',include:'values,formulas',tableMaxRows:15,tableMaxCols:15,maxChars:12000});
const err = await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A',options:{useRegex:true,maxResults:100},summary:'formula error scan'});
await fs.writeFile(`${outputDir}/detailed_inspect.txt`,check.ndjson,'utf8'); await fs.writeFile(`${outputDir}/detailed_errors.txt`,err.ndjson,'utf8');
for (const [name,range] of [['任务分解','A1:O20'],['里程碑','A1:H8'],['字典与说明','A1:F10']]) { const png = await wb.render({sheetName:name,range,scale:1,format:'png'}); await fs.writeFile(`${outputDir}/${name}.png`,new Uint8Array(await png.arrayBuffer())); }
const out = await SpreadsheetFile.exportXlsx(wb); await out.save(`${outputDir}/ALIS_开发任务分解表_detailed.xlsx`); console.log(JSON.stringify({taskCount:detail.length,errors:err.ndjson}));
