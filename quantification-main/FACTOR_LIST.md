# 可用因子列表（简版）

本文档只说明两件事：
- 当前系统里可以直接用于策略和回测的因子
- 建议后续新增的因子

## 1) 当前可用因子（回测可用）

### 量价基础
- `volume`：当日成交量
- `turnover`：当日成交额
- `change_pct`：当日涨跌幅绝对值

### 趋势与波动
- `momentum_5`：5日动量
- `momentum_20`：20日动量
- `volatility_20`：20日波动率
- `volume_ratio_5`：5日量比

### 市值与估值
- `float_mv`：流通市值
- `total_mv`：总市值
- `pe_ttm`：市盈率 TTM
- `pb`：市净率

### 交易状态
- `is_st`：是否 ST（1 是，0 否）
- `is_limit_up`：是否涨停（1 是，0 否）
- `is_limit_down`：是否跌停（1 是，0 否）
- `is_suspended`：是否停牌（1 是，0 否）

## 2) 暂不可用于回测的常见因子

以下因子在选股模块可能可用，但回测模块当前未实现：
- `in_hs300`
- `in_csi500`

> 若策略中使用上述因子，系统会在保存/回测时提示不支持。

## 3) 建议新增因子（优先级）

### 第一批（建议优先）
- `momentum_60`：60日中期动量
- `turnover_rate_5`：5日平均换手率
- `amplitude_20`：20日平均振幅
- `rsi_14`：14日 RSI
- `ma_bias_20`：价格相对20日均线偏离率

### 第二批（进阶）
- `beta_60`：相对基准的60日 Beta
- `max_drawdown_60`：60日最大回撤
- `skewness_20`：20日收益偏度
- `kurtosis_20`：20日收益峰度
- `downside_vol_20`：20日下行波动率

### 第三批（基本面扩展，依赖更多数据）
- `roe_ttm`：净资产收益率
- `gross_margin`：毛利率
- `revenue_yoy`：营收同比
- `profit_yoy`：净利润同比
- `debt_to_asset`：资产负债率

## 4) 使用建议（简短）

- 日频短线：优先 `momentum_5 + volume_ratio_5 + is_limit_up/is_suspended` 过滤
- 中短线：优先 `momentum_20 + volatility_20 + float_mv`
- 稳健型：优先 `volatility_20(负权重) + pb + pe_ttm`

