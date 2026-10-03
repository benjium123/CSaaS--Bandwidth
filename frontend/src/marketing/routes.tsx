import { Route, Routes } from "react-router-dom";
import { AlternativePage } from "@/marketing/pages/AlternativePage";
import { BlogIndexPage } from "@/marketing/pages/BlogIndexPage";
import { BlogPostPage } from "@/marketing/pages/BlogPostPage";
import { CalculatorPage } from "@/marketing/pages/CalculatorPage";
import { ComparePage } from "@/marketing/pages/ComparePage";
import { E911Page, FaqPage, SalesPage, SecurityPage } from "@/marketing/pages/InfoPages";
import { PrivacyPage, RefundPage, TermsPage } from "@/marketing/pages/LegalPages";
import { PricingPage } from "@/marketing/pages/PricingPage";
import { ProductPage } from "@/marketing/pages/ProductPage";
import { SolutionPage } from "@/marketing/pages/SolutionPage";
import { SwitchPage } from "@/marketing/pages/SwitchPage";

const MARKETING_PATH = /^\/(pricing|sales|faq|trust|legal\/911|legal\/privacy|legal\/terms|legal\/refunds|privacy|terms|refunds|calculator|switch|blog|blog\/[a-z0-9-]+|(product|solutions|compare|alternatives)\/[a-z0-9-]+)\/?$/;

/** True for the public website's pages; they render outside the signed-in app shell. */
export function isMarketingPath(pathname: string): boolean {
  return MARKETING_PATH.test(pathname);
}

export function MarketingRoutes() {
  return (
    <Routes>
      <Route path="/pricing" element={<PricingPage />} />
      <Route path="/calculator" element={<CalculatorPage />} />
      <Route path="/switch" element={<SwitchPage />} />
      <Route path="/sales" element={<SalesPage />} />
      <Route path="/faq" element={<FaqPage />} />
      <Route path="/trust" element={<SecurityPage />} />
      <Route path="/legal/911" element={<E911Page />} />
      <Route path="/legal/privacy" element={<PrivacyPage />} />
      <Route path="/legal/terms" element={<TermsPage />} />
      <Route path="/legal/refunds" element={<RefundPage />} />
      <Route path="/privacy" element={<PrivacyPage />} />
      <Route path="/terms" element={<TermsPage />} />
      <Route path="/refunds" element={<RefundPage />} />
      <Route path="/blog" element={<BlogIndexPage />} />
      <Route path="/blog/:slug" element={<BlogPostPage />} />
      <Route path="/product/:slug" element={<ProductPage />} />
      <Route path="/solutions/:slug" element={<SolutionPage />} />
      <Route path="/compare/:slug" element={<ComparePage />} />
      <Route path="/alternatives/:slug" element={<AlternativePage />} />
    </Routes>
  );
}
