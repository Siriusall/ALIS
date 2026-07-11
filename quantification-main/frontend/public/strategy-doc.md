## 自定义策略说明

本系统支持**日线多因子选股策略**。  
用户在前端“策略编写”里填写的内容，会作为 `strategies.content`（JSON）传到后端，由后端解析并执行。

支持两种主要写法：

- **简化权重写法（旧格式）**：只写 `volume` / `turnover` / `change_pct` 三个权重；
- **完整多因子写法（新格式，推荐）**：包含 `universe` / `filters` / `score` / `rebalance` 等字段。

---

## 一、简化权重写法（旧格式）

只针对三个基础因子设置权重，系统会自动归一化：

- `volume`：当日成交量
- `turnover`：当日成交额
- `change_pct`：当日涨跌幅绝对值

示例：

```json
{
  "volume": 0.4,
  "turnover": 0.4,
  "change_pct": 0.2
}
```

说明：

- 数字可以任意调整，系统会按比例自动归一化为权重；
- 这是当前系统内置的**默认策略**配置；
- 当你只写这 3 个键时，后端会自动升级成新格式来执行。

---

## 二、完整多因子写法（新格式，推荐）

新格式整体结构如下：

```json
{
  "universe": { ... },
  "filters": [ ... ],
  "score": {
    "factors": {
      "因子名": 权重数字
    }
  },
  "rebalance": {
    "freq": "daily | weekly | monthly",
    "top_n": 20
  },
  "lookback_days": 60
}
```

各字段含义：

- **`universe`**：选股空间（目前后端只做透传，可预留字段，示例：`{"index": "000300"}`）。
- **`filters`**：过滤条件数组，每个元素形如  
  `{"factor": "volume_ratio_5", "op": ">", "value": 1.5}`。
- **`score.factors`**：打分因子及权重，系统会自动做**权重归一化**再计算综合得分。
- **`rebalance.freq`**：调仓频率标记，取值 `"daily" | "weekly" | "monthly"`。
- **`rebalance.top_n`**：每次选出的前 N 只股票。
- **`lookback_days`**：为部分需要历史数据的因子准备的回溯天数（默认 60，取值会限制在 5–250 之间）。

> 注意：`score.factors` 中只能使用后端注册过的因子名；  
> `filters.factor` 也必须是这些名字之一。

---

## 三、当前支持的因子列表

后端的因子注册表（`FACTOR_REGISTRY`）中，已经注册了以下因子，可在 `score.factors` 和 `filters` 中使用：

### 1. 量价基础因子

- **`volume`**：当日成交量，越大越放量。
- **`turnover`**：当日成交额，越大代表资金参与度越高。
- **`change_pct`**：当日涨跌幅的绝对值，波动越大数值越高。

### 2. 动量（趋势）因子

- **`momentum_5`**：近 5 日价格动量，约等于 \((当前价 / 5 日前收盘) - 1\)。  
  适合捕捉短期强势股。
- **`momentum_20`**：近 20 日价格动量，约等于 \((当前价 / 20 日前收盘) - 1\)。  
  适合中期趋势跟踪。

### 3. 波动与量能结构

- **`volatility_20`**：近 20 日收益率波动率，越大代表波动越剧烈。
- **`volume_ratio_5`**：成交量相对 5 日平均量比，  
  计算方式：`今日成交量 / 最近 5 日平均成交量`，>1 表示放量，<1 表示缩量。

### 4. 估值 / 市值 / 状态（TuShare 快照）

- **`float_mv`**：流通市值。
- **`pe_ttm`**：市盈率 TTM。
- **`pb`**：市净率。
- **`is_st`**：是否 ST 股，是为 `1.0`，否则 `0.0`。
- **`is_suspended`**：是否停牌，是为 `1.0`。
- **`is_limit_up`**：是否涨停，是为 `1.0`。
- **`is_limit_down`**：是否跌停，是为 `1.0`。
- **`in_hs300`**：是否为沪深 300 成分股，是为 `1.0`。
- **`in_csi500`**：是否为中证 500 成分股，是为 `1.0`。

---

## 四、过滤条件（filters）写法

`filters` 是一个数组，每个元素表示一条过滤规则：

```json
{
  "factor": "volume_ratio_5",
  "op": ">",
  "value": 1.5
}
```

字段说明：

- **`factor`**：因子名（必须在「支持因子列表」中）。
- **`op`**：比较操作符，支持 `">" | ">=" | "<" | "<=" | "=="`。
- **`value`**：阈值，会被转成浮点数参与比较。

示例：剔除 ST、停牌、涨跌停，只保留量比放大的股票：

```json
"filters": [
  { "factor": "is_st", "op": "==", "value": 0 },
  { "factor": "is_suspended", "op": "==", "value": 0 },
  { "factor": "is_limit_up", "op": "==", "value": 0 },
  { "factor": "is_limit_down", "op": "==", "value": 0 },
  { "factor": "volume_ratio_5", "op": ">", "value": 1.5 }
]
```

---

## 五、常见策略示例

### 示例 1：默认三因子多空权重

逻辑：**量+额+波动** 三因子综合打分，偏好放量且波动适中的股票。

```json
{
  "universe": {},
  "filters": [],
  "score": {
    "factors": {
      "volume": 0.4,
      "turnover": 0.4,
      "change_pct": 0.2
    }
  },
  "rebalance": {
    "freq": "daily",
    "top_n": 20
  },
  "lookback_days": 60
}
```

> 等价于旧格式 `{ "volume": 0.4, "turnover": 0.4, "change_pct": 0.2 }`。

### 示例 2：动量+量比突破策略

逻辑：

- 近 20 日趋势向上（`momentum_20` 为正）；
- 今日明显放量（`volume_ratio_5 > 1.5`）；
- 剔除 ST / 停牌 / 涨跌停；
- 打分时更看重趋势，其次看量比。

```json
{
  "universe": {},
  "filters": [
    { "factor": "is_st", "op": "==", "value": 0 },
    { "factor": "is_suspended", "op": "==", "value": 0 },
    { "factor": "is_limit_up", "op": "==", "value": 0 },
    { "factor": "is_limit_down", "op": "==", "value": 0 },
    { "factor": "momentum_20", "op": ">", "value": 0 },
    { "factor": "volume_ratio_5", "op": ">", "value": 1.5 }
  ],
  "score": {
    "factors": {
      "momentum_20": 0.7,
      "volume_ratio_5": 0.3
    }
  },
  "rebalance": {
    "freq": "daily",
    "top_n": 20
  },
  "lookback_days": 60
}
```

### 示例 3：大盘低波动防御型策略

逻辑：

- 只在沪深 300 成分股里选（`in_hs300 = 1`）；
- 剔除 ST / 停牌；
- 偏好**低波动、成交额较大**的股票；
- 波动因子权重为负值，表示“越低越好”。

```json
{
  "universe": {},
  "filters": [
    { "factor": "in_hs300", "op": "==", "value": 1 },
    { "factor": "is_st", "op": "==", "value": 0 },
    { "factor": "is_suspended", "op": "==", "value": 0 }
  ],
  "score": {
    "factors": {
      "volatility_20": -0.5,
      "turnover": 0.3,
      "float_mv": 0.2
    }
  },
  "rebalance": {
    "freq": "weekly",
    "top_n": 15
  },
  "lookback_days": 60
}
```

---

## 六、调试与注意事项

- **权重自动归一化**：`score.factors` 里写的数字只代表相对大小，系统会自动按总和归一到 1。
- **负权重**：可以用负权重表达“越小越好”（例如 `volatility_20: -0.5`），内部会先按因子方向计算，再做一次综合归一化。
- **因子缺失**：如果某只股票在某个因子上计算失败或没有数据，该因子值会被视为 `0.0`，可能导致被过滤或得分偏低。
- **filters 先于打分**：先按 `filters` 过滤掉不符合条件的股票，再对剩下的股票做因子归一化和加权打分。

如果你有某种具体投资思路，比如「小市值+高动量」、「低估值+大盘蓝筹」、「缩量回调后放量突破」等，可以在这里先写成自然语言，然后对照本文件把它翻译成 JSON 策略配置。需要的话也可以把你的想法发给我，我可以帮你直接写出对应的完整策略 JSON。

