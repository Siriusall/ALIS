import { useEffect, useState } from "react";
import {
  fetchLiveMarketContext,
  fetchMarketContextGovernance,
  fetchMarketContextHistory,
  type LiveMarketContext,
  type MarketContextGovernance,
  type MarketContextHistory,
  type MarketProfile,
} from "../api/research";

const NAMES: Record<string, string> = {
  breadth: "上涨宽度",
  return: "赚钱效应",
  volume: "成交流动性",
  limit_up_down: "涨跌停结构",
  risk_preference: "风险偏好",
  leader_persistence: "龙头持续性",
  price_fundamental_alignment: "价格基本面一致性",
};

const STATES: Record<string, string> = {
  S0: "风险扩张",
  S1: "震荡分化",
  S2: "风险收缩",
  S3: "恐慌修复",
  bull: "上行",
  recovery: "复苏",
  sideways: "震荡",
  bear: "弱势",
  stress: "压力",
  risk_on: "偏进攻",
  neutral: "中性",
  risk_off: "偏防守",
};

function regimeReason(governance: MarketContextGovernance | null, fallback: string) {
  const hsmm = governance?.hsmm;
  if (!hsmm) return fallback;
  if (hsmm.production_eligible) return "HSMM已通过历史OOS、真实影子观察和上线评审，可按审批结果接管生产状态";
  if (hsmm.review?.status === "rejected") return "HSMM上线评审未通过，生产状态继续使用透明四状态机";
  if (hsmm.validation?.status === "passed") {
    if (hsmm.live_observation_weeks >= hsmm.minimum_live_observation_weeks) {
      return "HSMM历史OOS和真实影子观察期已通过，等待上线评审；评审前生产状态继续使用透明四状态机";
    }
    return `HSMM历史OOS已通过，正在进行真实影子观察（${hsmm.live_observation_weeks.toFixed(2)}/${hsmm.minimum_live_observation_weeks}周）；观察期和上线评审完成前生产状态继续使用透明四状态机`;
  }
  if (hsmm.history_years >= hsmm.minimum_years) {
    return "HSMM训练历史已满足六年要求，仍需通过历史OOS验证、真实影子观察和上线评审；当前生产状态使用透明四状态机";
  }
  return `HSMM训练历史仅${hsmm.history_years.toFixed(2)}年，尚未达到${hsmm.minimum_years}年要求；当前生产状态使用透明四状态机`;
}

function deriveMarketProfile(state: string, observations: Record<string, number>): MarketProfile {
  const breadth = observations.breadth ?? .5;
  const averageReturn = observations.average_return ?? 0;
  const volumeRatio = observations.volume_ratio ?? 1;
  const limitUp = observations.limit_up_ratio ?? 0;
  const limitDown = observations.limit_down_ratio ?? 0;
  const crowding = observations.crowding ?? 0;
  let code = "P04";
  if (state === "S0") code = crowding >= .65 || limitUp >= .05 ? "P03" : breadth >= .62 && averageReturn >= .008 ? "P01" : "P02";
  else if (state === "S2") code = averageReturn <= -.025 || limitDown >= .05 ? "P09" : volumeRatio <= .85 ? "P08" : "P07";
  else if (state === "S3") code = breadth >= .55 ? "P10" : volumeRatio >= 1.10 ? "P11" : "P12";
  else code = volumeRatio <= .85 ? "P06" : breadth <= .38 || breadth >= .62 || Math.abs(averageReturn) >= .015 ? "P05" : "P04";
  const labels: Record<string, string> = {P01:"全面扩张",P02:"流动性驱动",P03:"拥挤扩张",P04:"均衡震荡",P05:"宽度分化",P06:"缩量震荡",P07:"趋势收缩",P08:"流动性收缩",P09:"恐慌抛售",P10:"宽度修复",P11:"流动性修复",P12:"脆弱反弹"};
  const multipliers: Record<string, number> = {P01:1,P02:.98,P03:.90,P04:1,P05:.95,P06:.90,P07:.95,P08:.90,P09:.85,P10:1,P11:.98,P12:.90};
  const separation = Math.max(Math.abs(breadth-.5)*2, Math.min(1,Math.abs(averageReturn)/.03), Math.min(1,Math.abs(volumeRatio-1)), Math.min(1,Math.max(limitUp,limitDown)/.08), Math.min(1,crowding));
  return {code,label:labels[code],parent_state:state,parent_label:STATES[state]??state,confidence:Math.min(.9,.55+.35*separation),risk_budget_multiplier:multipliers[code],risk_ceiling_policy:"cannot_exceed_parent_state",dimensions:[breadth>=.6?"宽度偏强":breadth<=.4?"宽度偏弱":"宽度中性",averageReturn>=.005?"上涨":averageReturn<=-.005?"下跌":"收益平稳",volumeRatio>=1.1?"放量":volumeRatio<=.9?"缩量":"流动性正常",crowding>=.65?"拥挤":"拥挤度可控",limitDown>=.03?"跌停压力":limitUp>=.03?"涨停扩散":"涨跌停平稳"],method:"hierarchical_market_profile_v1",profile_count:12};
}

export default function SentimentPage() {
  const [data, setData] = useState<LiveMarketContext | null>(null);
  const [history, setHistory] = useState<MarketContextHistory | null>(null);
  const [governance, setGovernance] = useState<MarketContextGovernance | null>(null);
  const [error, setError] = useState("");

  async function load() {
    try {
      const [live, past, modelGovernance] = await Promise.allSettled([
        fetchLiveMarketContext(),
        fetchMarketContextHistory(),
        fetchMarketContextGovernance(),
      ]);
      if (live.status === "fulfilled") setData(live.value);
      if (past.status === "fulfilled") setHistory(past.value);
      if (modelGovernance.status === "fulfilled") setGovernance(modelGovernance.value);
      const failed = [live, past, modelGovernance].find((item) => item.status === "rejected");
      setError(
        failed?.status === "rejected"
          ? failed.reason instanceof Error
            ? failed.reason.message
            : String(failed.reason)
          : "",
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const currentProfile = data ? data.market_profile ?? deriveMarketProfile(data.market_state.state, data.observations) : null;

  return (
    <section className="sentiment-page">
      <div className="product-module-head">
        <div>
          <span>MARKET SENTIMENT</span>
          <h1>市场情绪中心</h1>
          <p>这里只判断节奏和仓位，不替代企业基本面判断。</p>
        </div>
        <button className="button ghost" onClick={() => void load()}>
          刷新
        </button>
      </div>

      {error && <div className="alert">{error}</div>}

      {data && (
        <>
          <div className="sentiment-hero">
            <div className="sentiment-score">
              <strong>{data.sentiment.score.toFixed(1)}</strong>
              <span>情绪得分</span>
              <em>拥挤扣分 {data.sentiment.crowding_penalty.toFixed(1)}</em>
            </div>
            <div>
              <span>市场状态</span>
              <h2>{STATES[data.market_state.state] ?? data.market_state.state}</h2>
              <p>置信度 {(data.market_state.confidence * 100).toFixed(1)}% · {data.market_state.model}</p>
              <em>{regimeReason(governance, data.market_state.reason)}</em>
            </div>
            <div className="sentiment-profile">
              <span>市场画像 · 12类子状态</span>
              <h2>{currentProfile?.label ?? "正在识别"}</h2>
              <p>
                {currentProfile
                  ? `${currentProfile.code} · 置信度 ${(currentProfile.confidence * 100).toFixed(1)}% · 风险微调 ${(currentProfile.risk_budget_multiplier * 100).toFixed(0)}%`
                  : "后端更新后生成层级市场画像"}
              </p>
              <div className="sentiment-profile-tags">
                {currentProfile?.dimensions.map((item) => <i key={item}>{item}</i>)}
              </div>
            </div>
            <div>
              <span>数据切片</span>
              <h2>{data.trade_date}</h2>
              <p>{data.evidence.symbols} 只股票 · {data.evidence.amount_dates} 个成交额日期</p>
            </div>
          </div>

          {governance?.hsmm && (
            <section className="workspace-card" style={{ marginTop: 16 }}>
              <div className="section-kicker">MODEL GOVERNANCE</div>
              <h2>HSMM 影子观察</h2>
              <p>
                当前生产状态：透明四状态机；HSMM 仅影子运行，不参与仓位门控。
              </p>
              <div className="sentiment-components">
                <article><span>历史 OOS</span><strong>{governance.hsmm.validation?.status === "passed" ? "已通过" : "未通过"}</strong></article>
                <article><span>真实观察期</span><strong>{governance.hsmm.live_observation_weeks.toFixed(2)} / {governance.hsmm.minimum_live_observation_weeks} 周</strong></article>
                <article><span>上线评审</span><strong>{governance.hsmm.review?.status ?? "未完成"}</strong></article>
                <article><span>当前模型</span><strong>{governance.production_model}</strong></article>
              </div>
              <div className="module-missing">{governance.hsmm.blocking_reason}</div>
            </section>
          )}

          <div className="sentiment-components">
            {Object.entries(data.sentiment.components)
              .filter(([key]) => key !== "limit_up_down")
              .map(([key, value]) => (
                <article key={key}>
                  <span>{NAMES[key] ?? key}</span>
                  <strong>{(50 + value * 50).toFixed(1)}</strong>
                  <div>
                    <i style={{ width: `${Math.max(0, Math.min(100, 50 + value * 50))}%` }} />
                  </div>
                </article>
              ))}
          </div>

          <div className="product-module-grid">
            <article className="product-module-card">
              <h2>真实观测</h2>
              {Object.entries(data.observations).map(([key, value]) => (
                <div className="module-output" key={key}>
                  <i />
                  {NAMES[key] ?? key} · {Number(value).toFixed(4)}
                </div>
              ))}
            </article>
            <article className="product-module-card">
              <h2>固定权重</h2>
              {Object.entries(data.sentiment.weights).map(([key, value]) => (
                <div className="module-output" key={key}>
                  <i />
                  {NAMES[key] ?? key} · {(value * 100).toFixed(0)}%
                </div>
              ))}
            </article>
            <article className="product-module-card">
              <h2>数据限制</h2>
              {data.limitations.map((item) => (
                <div className="module-missing" key={item}>
                  {item}
                </div>
              ))}
            </article>
          </div>

          {history && (
            <div className="card decision-panel">
              <h3>历史状态与切换</h3>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>交易日</th>
                      <th>状态</th>
                      <th>市场画像</th>
                      <th>置信度</th>
                      <th>情绪</th>
                      <th>可用时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.items.slice(-30).reverse().map((item) => (
                      <tr key={item.trade_date}>
                        <td>{item.trade_date}</td>
                        <td>{STATES[item.market_state] ?? item.market_state}</td>
                        <td>{(item.market_profile ?? deriveMarketProfile(item.market_state, item.observations)).label}</td>
                        <td>{(item.state_confidence * 100).toFixed(1)}%</td>
                        <td>{item.sentiment_score.toFixed(1)}</td>
                        <td>{item.available_time}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
