import { lazy, useCallback, useEffect, useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { isSectionId, type SectionId } from "../components/Layout";
import { useAuth } from "../context/AuthContext";
import Layout from "../components/Layout";
const StrategyPage = lazy(() => import("../components/StrategyPage"));
const PositionPage = lazy(() => import("../components/PositionPage"));
const DecisionAnalysisPage = lazy(() => import("../components/DecisionAnalysisPage"));
const InvestorDashboardPage = lazy(() => import("../components/InvestorDashboardPage"));
const DataCenterPage = lazy(() => import("../components/DataCenterPage"));
const ProductModulePage = lazy(() => import("../components/ProductModulePage"));
const ResearchWorkbenchPage = lazy(() => import("../components/ResearchWorkbenchPage"));
const StockResearchPage = lazy(() => import("../components/StockResearchPage"));
const ThesisPage = lazy(() => import("../components/ThesisPage"));
const IndustryResearchPage = lazy(() => import("../components/IndustryResearchPage"));
const EventCenterPage = lazy(() => import("../components/EventCenterPage"));
const SentimentPage = lazy(() => import("../components/SentimentPage"));
const ExpectationPage = lazy(() => import("../components/ExpectationPage"));
const ValuationPage = lazy(() => import("../components/ValuationPage"));
const PositionRiskPage = lazy(() => import("../components/PositionRiskPage"));
const PortfolioWorkbenchPage = lazy(() => import("../components/PortfolioWorkbenchPage"));
const GovernancePage = lazy(() => import("../components/GovernancePage"));
const DecisionAuditPage = lazy(() => import("../components/DecisionAuditPage"));
const StockPoolPage = lazy(() => import("../components/StockPoolPage"));
const ReviewPage = lazy(() => import("../components/ReviewPage"));
const ReviewWorkbenchPage = lazy(() => import("../components/ReviewWorkbenchPage"));
const TradeHistory = lazy(() => import("../components/TradeHistory"));
const BackendLogsPage = lazy(() => import("../components/BackendLogsPage"));
const CandidatePoolPage = lazy(() => import("../components/CandidatePoolPage"));
const StrategyLabPage = lazy(() => import("../components/StrategyLabPage"));
const FactorLabPage = lazy(() => import("../components/FactorLabPage"));

export default function DashboardPage() {
  const { user, logout } = useAuth();
  const { section } = useParams();
  const navigate = useNavigate();
  const currentSection: SectionId = isSectionId(section) ? section : "market";

  useEffect(() => {
    if (!isSectionId(section)) navigate("/app/market", { replace: true });
  }, [navigate, section]);

  const setCurrentSection = useCallback((next: SectionId) => navigate(`/app/${next}`), [navigate]);
  const sections = useMemo(() => ({
    market: <InvestorDashboardPage focus="returns" />,
    research: <ResearchWorkbenchPage onNavigate={setCurrentSection} />,
    dataCenter: <DataCenterPage />,
    stockResearch: <StockResearchPage onNavigate={setCurrentSection} />,
    industryResearch: <IndustryResearchPage />,
    events: <EventCenterPage />,
    sentiment: <SentimentPage />,
    themes: <ProductModulePage capabilityKey="themes" />,
    expectation: <ExpectationPage />,
    valuation: <ValuationPage />,
    portfolioWorkbench: <InvestorDashboardPage focus="rebalance" />,
    stockPool: <StockPoolPage />,
    thesis: <ThesisPage />,
    portfolio: <PositionPage onNavigate={setCurrentSection} />,
    executionLedger: <TradeHistory onNavigate={setCurrentSection} />,
    positionRisk: <PositionRiskPage onNavigate={setCurrentSection} />,
    reviewWorkbench: <InvestorDashboardPage focus="models" />,
    candidatePool: <CandidatePoolPage />,
    strategyLab: <StrategyLabPage />,
    factorLab: <FactorLabPage />,
    validation: <DecisionAnalysisPage onNavigate={setCurrentSection} />,
    decisionLogs: <DecisionAuditPage />,
    review: <ReviewPage />,
    governance: <GovernancePage />,
    strategy: <StrategyPage />,
    backendLogs: <BackendLogsPage />,
  }), [setCurrentSection]);

  const header = (
    <>
      <div className="header">
        <div><h1>ALIS 长线投资决策系统</h1></div>
      </div>
    </>
  );

  if (!user) return null;

  return (
    <Layout
      header={header}
      sections={sections}
      currentSection={currentSection}
      onSectionChange={setCurrentSection}
      user={user}
      onLogout={logout}
      onLoginClick={() => {}}
      onRegisterClick={() => {}}
    />
  );
}
