import { useEffect, useState } from "react";
import type { SectionId } from "../components/Layout";
import type { LogEntry } from "../types";
import { fetchLogs } from "../api/market";
import { useAuth } from "../context/AuthContext";
import Layout from "../components/Layout";
import StrategyPage from "../components/StrategyPage";
import FactorLibraryPage from "../components/FactorLibraryPage";
import PositionPage from "../components/PositionPage";
import TaskLogs from "../components/TaskLogs";
import QuantRunPanel from "../components/QuantRunPanel";
import DecisionPage from "../components/DecisionPage";
import DataCenterPage from "../components/DataCenterPage";
import ProductModulePage from "../components/ProductModulePage";
import StockResearchPage from "../components/StockResearchPage";
import ThesisPage from "../components/ThesisPage";
import IndustryResearchPage from "../components/IndustryResearchPage";
import EventCenterPage from "../components/EventCenterPage";
import SentimentPage from "../components/SentimentPage";
import ExpectationPage from "../components/ExpectationPage";
import ValuationPage from "../components/ValuationPage";

export default function DashboardPage() {
  const { user, logout } = useAuth();
  const [currentSection, setCurrentSection] = useState<SectionId>("market");
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    fetchLogs().then((data) => {
      if (active) setLogs(Array.isArray(data) ? data : []);
    });
    return () => {
      active = false;
    };
  }, []);

  // 切换到「任务日志」时重新拉取，避免只显示进入页面的旧数据
  useEffect(() => {
    if (currentSection !== "decisionLogs") return;
    fetchLogs().then((data) => setLogs(Array.isArray(data) ? data : []));
  }, [currentSection]);

  const header = (
    <>
      <div className="header">
        <div>
          <h1>ALIS 研究与组合决策平台</h1>
        </div>
      </div>
      {error && <div className="alert">{error}</div>}
    </>
  );

  if (!user) return null;

  return (
    <Layout
      header={header}
      currentSection={currentSection}
      onSectionChange={setCurrentSection}
      user={user}
      onLogout={logout}
      onLoginClick={() => {}}
      onRegisterClick={() => {}}
    >
      <DecisionPage onNavigate={setCurrentSection} />
      <DataCenterPage />
      <StockResearchPage />
      <IndustryResearchPage />
      <EventCenterPage />
      <SentimentPage />
      <ProductModulePage capabilityKey="themes" />
      <ExpectationPage />
      <ValuationPage />
      <FactorLibraryPage />
      <ThesisPage />
      <PositionPage />
      <ProductModulePage capabilityKey="risk" />
      <QuantRunPanel />
      <TaskLogs logs={logs} />
      <ProductModulePage capabilityKey="review" />
      <ProductModulePage capabilityKey="governance" />
      <StrategyPage />
    </Layout>
  );
}
