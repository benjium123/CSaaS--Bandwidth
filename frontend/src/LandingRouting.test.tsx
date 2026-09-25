import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";

const auth = vi.hoisted(() => ({ ready: false, me: null as any, orgId: null }));
vi.mock("@/auth/AuthContext", () => ({ useAuth: () => auth }));
vi.mock("@/pages/SignUpPage", () => ({ SignUpPage: () => <h1>Signup destination</h1> }));
beforeEach(() => { auth.ready = false; auth.me = null; localStorage.clear(); });
function open(path: string) {
  return render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>);
}

describe("Public landing routes", () => {
  it("renders the homepage before authentication resolves and links to signbox", () => {
    open("/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Small ring.");
    expect(screen.getByRole("link", { name: "Get started" })).toHaveAttribute("href", "/signbox");
    expect(screen.getAllByText("MOST POPULAR").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: /Compare plans/ })).toHaveAttribute("href", "/pricing");
    fireEvent.click(screen.getByRole("button", { name: "Messages" }));
    expect(screen.getByText("Texting after messaging registration approval")).toBeInTheDocument();
  });
  it.each(["/pricing", "/faq", "/trust", "/legal/911", "/product/calling", "/solutions/real-estate", "/compare/quo", "/sales"])(
    "renders the public page %s without signing in", path => {
      open(path);
      expect(screen.getAllByRole("heading", { level: 1 }).length).toBe(1);
      expect(screen.getAllByRole("link", { name: /Get started/ }).length).toBeGreaterThan(0);
    },
  );
  it.each(["/signbox", "/signup"])("opens signup from %s", async path => {
    auth.ready = true;
    open(path);
    expect(await screen.findByRole("heading", { name: "Signup destination" })).toBeInTheDocument();
  });
});

