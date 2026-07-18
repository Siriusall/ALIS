# 前端收尾清单

更新时间：2026-07-17

这份清单只记录“剩余 5%”的收尾项，不再包含大结构重做。当前主路径已经基本切到“数据中台 + dashboard + 今日调仓建议 + 人工复核”。

## 已经基本达标

- 主导航结构
- 数据中台首页
- 今日调仓建议主路径
- 单股研究工作台
- 股票池 / 组合与执行 / 人工复核
- 高频卡片、动作条、队列卡的主要视觉层级

## P1：主路径最后一轮 polish

### 1. 今日调仓建议页

文件：

- `frontend/src/components/DecisionPage.tsx`

剩余点：

- 检查 `market-overview-strip` 在真实数据较长时是否会显得拥挤。
- 评估“市场环境证据 / 主题与组合解释 / 执行前检查”三块是否还需要进一步压缩标题和按钮文案。
- 检查候选标的分数展示是否需要更轻，避免数字过抢眼。

完成标准：

- 用户第一屏先看到结论、风险、动作，不会先被解释文字吸引。

### 2. 单股研究工作台

文件：

- `frontend/src/components/StockResearchPage.tsx`

剩余点：

- `EVIDENCE & LIMITATIONS` 区仍偏工具区，后续可考虑进一步压缩说明性内容。
- 多维研究状态卡和估值区虽然已统一，但仍可再检查是否过密。
- 评估锚点导航是否需要进一步减少文案长度。

完成标准：

- 单股研究页整体更像“工作台”，不是“研究报告页”。

### 3. 股票池

文件：

- `frontend/src/components/StockPoolPage.tsx`

剩余点：

- `pool-gates` 和 `pool-member-list` 仍保留一定工具感。
- 可再检查“检查并登记标的”表单区域的紧凑度。

完成标准：

- 观察池 → 核心候选 → 持仓 的路径非常直观。

## P2：低频页统一感

### 4. 行业研究页

文件：

- `frontend/src/components/IndustryResearchPage.tsx`

剩余点：

- 指标明细仍有一些旧式 `small` 层级。
- 卡片内部还是偏研究工具页，不是问题，但可再统一副信息风格。

### 5. 估值页

文件：

- `frontend/src/components/ValuationPage.tsx`

剩余点：

- 页面已经整洁，但仍偏分析工具。
- 可进一步检查估值 hero 区在窄屏和长数字场景下的观感。

### 6. 预期变化页

文件：

- `frontend/src/components/ExpectationPage.tsx`

剩余点：

- 证据卡片仍稍微偏说明页。
- 可以再检查 “系统预测基线 / 公司业绩预告” 的信息密度是否还需要降一点。

### 7. 情绪页 / 事件页 / 投资逻辑页

文件：

- `frontend/src/components/SentimentPage.tsx`
- `frontend/src/components/EventCenterPage.tsx`
- `frontend/src/components/ThesisPage.tsx`

剩余点：

- 已经基本统一，后续只需要做点状 polish。
- 不建议再重构结构，只需要顺手处理局部字重和留白。

## P3：技术后台区

### 8. 数据中台技术控制台

文件：

- `frontend/src/components/DataCenterTechnicalConsole.tsx`
- `frontend/src/components/DataCenterTechnicalConsole.css`

现状判断：

- 已经比之前统一很多。
- 但它天然承担排障/运维职责，信息密度高是正常的。

剩余点：

- 表格区和资产目录仍偏工具后台。
- 不建议强行改成首页 dashboard 风格，否则会损失排障信息。
- 只建议继续统一副信息样式，不建议再砍核心字段。

完成标准：

- 技术页和主路径看起来属于同一个产品，但保留技术页应有的信息密度。

## 不建议再做的事

- 不建议再次扩大主导航改造范围。
- 不建议再新增前端模块。
- 不建议把技术控制台强行改成展示页。
- 不建议为了一致性牺牲排障信息。

## 下一步建议

如果继续收尾，顺序建议是：

1. `DecisionPage.tsx`
2. `StockResearchPage.tsx`
3. `StockPoolPage.tsx`
4. `IndustryResearchPage.tsx`
5. `DataCenterTechnicalConsole.tsx`

## 当前验证状态

- 已执行 `npm run build`
- 前端构建通过
- 当前判断：主路径体验已达标，剩余工作以低频页 polish 为主
