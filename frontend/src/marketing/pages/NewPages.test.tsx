import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "@/App";

const auth = vi.hoisted(() => ({ ready: false, me: null as any, orgId: null }));
vi.mock("@/auth/AuthContext", () => ({ useAuth: () => auth }));
vi.mock("@/pages/SignUpPage", () => ({ SignUpPage: () => <h1>Signup destination</h1> }));
beforeEach(() => { auth.ready = false; auth.me = null; localStorage.clear(); });
function open(path: string) {
  return render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>);
}

const NEW_PAGES = ["/calculator", "/switch", "/alternatives/quo", "/alternatives/ringcentral", "/alternatives/aircall"];

describe("New marketing pages", () => {
  it.each(NEW_PAGES)("renders %s with one h1 and a Get started link", path => {
    open(path);
    expect(screen.getAllByRole("heading", { level: 1 }).length).toBe(1);
    expect(screen.getAllByRole("link", { name: /Get started/ }).length).toBeGreaterThan(0);
  });

  it("shows the calc-users slider on /calculator", () => {
    open("/calculator");
    expect(document.getElementById("calc-users")).toHaveAttribute("type", "range");
  });

  it("shows the Quo comparison on /alternatives/quo", () => {
    open("/alternatives/quo");
    expect(screen.getByText("Why teams switch from Quo")).toBeInTheDocument();
  });

  it("redirects an unknown alternative to the homepage", () => {
    open("/alternatives/nope");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Your team.");
  });
});
