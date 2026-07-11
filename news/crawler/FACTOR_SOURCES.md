# A-share factor crawler source map

This crawler uses browser-rendered web pages only. If visible DOM text is missing or unreliable, quote pages can fall back to screenshot OCR through Tesseract.

## Current sources

| Group | Field | Current source | Status |
| --- | --- | --- | --- |
| 成长能力 | 营收同比 | Eastmoney F10 财务分析 | implemented |
| 成长能力 | 扣非净利同比 | Eastmoney F10 财务分析 | implemented |
| 成长能力 | 近 3 年 CAGR | Eastmoney F10 财务分析 revenue series | implemented |
| 盈利质量 | 毛利率 | Eastmoney F10 财务分析 | implemented |
| 盈利质量 | 净利率 | Eastmoney F10 财务分析 | implemented |
| 盈利质量 | ROE | Eastmoney F10 财务分析 | implemented |
| 盈利质量 | ROIC | Eastmoney F10 财务分析, currently using total asset return fallback | partial |
| 现金流质量 | 经营现金流/净利润 | Eastmoney F10 财务分析 cash-flow ratio fallback | partial |
| 现金流质量 | 自由现金流 | Eastmoney F10 现金流量表 | partial, depends on visible cash-flow rows |
| 偿债安全 | 资产负债率 | Eastmoney F10 财务分析 | implemented |
| 偿债安全 | 有息负债 | Eastmoney F10 balance sheet debt rows | partial |
| 偿债安全 | 货币资金覆盖 | Eastmoney F10 cash/debt rows | partial |
| 股东回报 | 股息率 | Sina/THS quote text or OCR | partial |
| 股东回报 | 分红率 | Eastmoney F10 cash-flow/dividend rows | partial |
| 股东回报 | 回购金额 | Eastmoney F10 balance sheet treasury stock fallback | partial |
| 稳定性 | 过去 5 年业绩波动 | Eastmoney F10 profit series | implemented |
| 稳定性 | 周期性 | derived from profit volatility | implemented |
| 估值 | PE | Sina/THS quote text or OCR | implemented |
| 估值 | PB | Sina/THS quote text or OCR | partial |
| 估值 | PS | Sina/THS quote text or OCR | partial |
| 估值 | 股息率 | Sina/THS quote text or OCR | partial |
| 估值 | 估值分位 | not filled until a real valuation history page/source is added | missing |
| 机构认可 | 公募持仓 | Eastmoney F10 股东研究 | implemented |
| 机构认可 | 北向 | Eastmoney F10 股东研究 | implemented |
| 机构认可 | ETF | Eastmoney F10 股东研究 institution details | implemented |
| 机构认可 | 调研次数 | pending visible research page/OCR source | missing |

## Useful local checks

```powershell
python main.py single 600519 --market SH --summary
python main.py crawl --codes 600519,000001
python main.py coverage
python probe_web_sources.py --code 600519 --screenshot
```
