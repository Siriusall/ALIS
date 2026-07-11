import { useState } from "react";
import Layout, { SectionId } from "./components/Layout";
import CrawlPage from "./pages/CrawlPage";
import FactorQueryPage from "./pages/FactorQueryPage";
import VerifyPage from "./pages/VerifyPage";

export default function App() {
  const [section, setSection] = useState<SectionId>("factors");

  const header = (
    <div className="header">
      <div>
        <h1>因子系统</h1>
        <p className="text-muted" style={{ margin: 0, fontSize: 13 }}>
          沪深股票因子网页爬取、覆盖率监控与查询
        </p>
      </div>
    </div>
  );

  return (
    <Layout header={header} currentSection={section} onSectionChange={setSection}>
      <FactorQueryPage />
      <CrawlPage />
      <VerifyPage />
    </Layout>
  );
}
