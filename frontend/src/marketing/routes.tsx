import { Route, Routes } from "react-router-dom";
import { ComparePage } from "@/marketing/pages/ComparePage";
import { E911Page, FaqPage, SalesPage, SecurityPage } from "@/marketing/pages/InfoPages";
import { PricingPage } from "@/marketing/pages/PricingPage";
import { ProductPage } from "@/marketing/pages/ProductPage";
import { SolutionPage } from "@/marketing/pages/SolutionPage";

const MARKETING_PATH = /^\/(pricing|sales|faq|trust|legal\/911|(product|solutions|compare)\/[a-z0-9-]+)\/?$/;

/** True for the public website's pages; they render outside the signed-in app shell. */
export function isMarketingPath(pathname: string): boolean {
  return MARKETING_PATH.test(pathname);
}

export function MarketingRoutes() {
  return (
    <Routes>
      <Route path="/pricing" element={<PricingPage />} />
      <Route path="/sales" element={<SalesPage />} />
      <Route path="/faq" element={<FaqPage />} />
      <Route path="/trust" element={<SecurityPage />} />
      <Route path="/legal/911" element={<E911Page />} />
      <Route path="/product/:slug" element={<ProductPage />} />
      <Route path="/solutions/:slug" element={<SolutionPage />} />
      <Route path="/compare/:slug" element={<ComparePage />} />
    </Routes>
  );
}
