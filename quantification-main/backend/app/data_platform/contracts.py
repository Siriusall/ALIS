"""Business-facing data contracts. Thresholds are explicit and reviewable."""

CONTRACTS = {
    "decision": {
        "name": "交易决策", "description": "生成当日选股和交易建议前的数据条件",
        "requirements": [
            ("trade_calendar", "交易日历", 1.0, "block"),
            ("latest_daily", "最新日线行情", 0.995, "block"),
            ("latest_valuation", "最新估值", 0.95, "degrade"),
            ("limit_price", "涨跌停价格", 0.99, "block"),
            ("suspension", "停复牌记录", 0.99, "block"),
            ("capital_structure", "北向/两融/龙虎榜", 0.99, "degrade"),
            ("moneyflow", "个股资金流", 0.99, "degrade"),
        ],
    },
    "quant_analysis": {
        "name": "量化分析", "description": "因子计算、股票筛选和模型评分的数据条件",
        "requirements": [
            ("latest_daily", "最新日线行情", 0.99, "block"),
            ("latest_valuation", "最新估值", 0.90, "degrade"),
            ("financial", "财报采集切片", 0.90, "degrade"),
            ("candidate_financial_fields", "候选研究字段", 1.0, "degrade"),
            ("stock_master", "股票主数据", 0.99, "block"),
            ("capital_structure", "资金结构历史", 0.99, "degrade"),
            ("moneyflow", "个股资金流历史", 0.99, "degrade"),
            ("major_news", "叙事新闻历史", 0.99, "degrade"),
            ("theme_snapshots", "主题快照历史", 0.99, "degrade"),
        ],
    },
    "backtest": {
        "name": "历史回测", "description": "历史策略回放必须避免缺口、复权错误和未来数据泄漏",
        "requirements": [
            ("history_calendar", "交易日历", 1.0, "block"),
            ("history_daily", "日线行情", 0.995, "block"),
            ("adj_factor", "复权因子", 0.995, "block"),
            ("history_suspension", "停复牌记录", 0.99, "block"),
            ("history_limit", "涨跌停价格", 0.99, "block"),
            ("stock_history", "股票历史状态", 0.99, "block"),
            ("capital_structure", "资金结构历史", 0.99, "block"),
            ("moneyflow", "个股资金流历史", 0.99, "block"),
            ("etf_history", "ETF价格与净值历史", 0.99, "block"),
            ("etf_pcf", "ETF每日申购赎回篮子", 0.80, "degrade"),
        ],
    },
    "position_risk": {
        "name": "持仓风控", "description": "持仓估值、交易约束和风险事件监控的数据条件",
        "requirements": [
            ("latest_daily", "最新行情", 0.995, "block"),
            ("suspension", "停复牌记录", 1.0, "block"),
            ("limit_price", "涨跌停价格", 0.99, "block"),
            ("announcements", "最新公告", 0.90, "degrade"),
            ("major_news", "新闻叙事历史", 0.99, "degrade"),
        ],
    },
    "model_training": {
        "name": "模型训练", "description": "训练特征、标签和样本时间范围的数据条件",
        "requirements": [
            ("history_daily", "历史行情特征", 0.995, "block"),
            ("adj_factor", "复权因子", 0.995, "block"),
            ("financial", "财务特征", 0.90, "block"),
            ("point_in_time", "时点可用性", 1.0, "block"),
            ("capital_structure", "资金结构训练样本", 0.99, "block"),
            ("etf_history", "ETF轮动训练样本", 0.99, "block"),
            ("etf_pcf", "ETF真实篮子核验", 0.80, "degrade"),
            ("major_news", "叙事训练语料", 0.99, "block"),
            ("theme_snapshots", "叙事状态快照", 1.0, "block"),
        ],
    },
    "narrative_model": {
        "name": "叙事状态模型", "description": "训练叙事生命周期、退潮和退出信号的数据条件",
        "requirements": [
            ("history_daily", "历史行情特征", 0.995, "block"),
            ("moneyflow", "资金斜率历史", 0.99, "degrade"),
            ("major_news", "叙事新闻历史", 0.99, "degrade"),
            ("theme_snapshots", "250日主题快照", 1.0, "block"),
        ],
    },
    "financial_research": {
        "name": "财务研究", "description": "财报、财务指标和披露时间的数据条件",
        "requirements": [
            ("financial", "财务报表采集切片", 0.90, "degrade"),
            ("financial_indicator", "财务指标采集切片", 0.90, "degrade"),
            ("candidate_financial_fields", "候选池研究字段", 1.0, "degrade"),
            ("announcements", "财报公告", 0.90, "degrade"),
            ("point_in_time", "披露时点", 1.0, "block"),
        ],
    },
}
