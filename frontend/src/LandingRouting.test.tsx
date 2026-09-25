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
    // Four people: Team (3 users + 3 numbers, $45) plus one $15 user and one $5 number.
    fireEvent.click(screen.getByRole("button", { name: "Add a person" }));
    expect(screen.getByRole("status", { name: "People" })).toHaveTextContent("4");
    expect(screen.getByText("Team plan + add-ons")).toBeInTheDocument();
    expect(screen.getByText("$65", { exact: false })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Messages" }));
    expect(screen.getByText("Texting after messaging registration approval")).toBeInTheDocument();
  });
  it.each(["/signbox", "/signup"])("opens signup from %s", async path => {
    auth.ready = true;
    open(path);
    expect(await screen.findByRole("heading", { name: "Signup destination" })).toBeInTheDocument();
  });
});

