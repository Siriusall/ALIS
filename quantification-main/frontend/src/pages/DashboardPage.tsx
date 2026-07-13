import { lazy, useEffect, useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { isSectionId, type SectionId } from "../components/Layout";
import { useAuth } from "../context/AuthContext";
import Layout from "../components/Layout";
const StrategyPage = lazy(() => import("../components/StrategyPage"));
const PositionPage = lazy(() => import("../components/PositionPage"));
const QuantRunPanel = lazy(() => import("../components/QuantRunPanel"));
const DecisionPage = lazy(() => import("../components/DecisionPage"));
const DataCenterPage = lazy(() => import("../components/DataCenterPage"));
const ProductModulePage = lazy(() => import("../components/ProductModulePage"));
const StockResearchPage = lazy(() => import("../components/StockResearchPage"));
const ThesisPage = lazy(() => import("../components/ThesisPage"));
const IndustryResearchPage = lazy(() => import("../components/IndustryResearchPage"));
const EventCenterPage = lazy(() => import("../components/EventCenterPage"));
const SentimentPage = lazy(() => import("../components/SentimentPage"));
const ExpectationPage = lazy(() => import("../components/ExpectationPage"));
const ValuationPage = lazy(() => import("../components/ValuationPage"));
const PositionRiskPage = lazy(() => import("../components/PositionRiskPage"));
const GovernancePage = lazy(() => import("../components/GovernancePage"));
const DecisionAuditPage = lazy(() => import("../components/DecisionAuditPage"));
const StockPoolPage = lazy(() => import("../components/StockPoolPage"));
const ReviewPage = lazy(() => import("../components/ReviewPage"));

export default function DashboardPage() {
  const { user, logout } = useAuth();
  const { section } = useParams();
  const navigate = useNavigate();
  const currentSection: SectionId = isSectionId(section) ? section : "market";

  useEffect(() => {
    if (!isSectionId(section)) navigate("/app/market", { replace: true });
  }, [navigate, section]);

  const setCurrentSection = (next: SectionId) => navigate(`/app/${next}`);
  const sections = useMemo(() => ({
    market: <DecisionPage onNavigate={setCurrentSection} />,
    dataCenter: <DataCenterPage />,
    stockResearch: <StockResearchPage />,
    industryResearch: <IndustryResearchPage />,
    events: <EventCenterPage />,
    sentiment: <SentimentPage />,
    themes: <ProductModulePage capabilityKey="themes" />,
    expectation: <ExpectationPage />,
    valuation: <ValuationPage />,
    stockPool: <StockPoolPage />,
    thesis: <ThesisPage />,
    portfolio: <PositionPage />,
    positionRisk: <PositionRiskPage />,
    validation: <QuantRunPanel />,
    decisionLogs: <DecisionAuditPage />,
    review: <ReviewPage />,
    governance: <GovernancePage />,
    strategy: <StrategyPage />,
  }), []);

  const header = (
    <>
      <div className="header">
        <div>
          <h1>ALIS 研究与组合决策平台</h1>
        </div>
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
