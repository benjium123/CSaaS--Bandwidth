import { describe, it, expect } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CustomerAccountsTab } from "./CustomerAccountsTab";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const account = { id: "customer-1", email: "customer@example.com", full_name: "New Customer", created_at: "2026-09-22", is_active: true, email_verified: false, is_operator: false, workspaces: [{ id: "org-1", name: "Customer workspace", account_type: "individual", role: "owner", status: "draft" }] };
function setup(blockers: string[] = []) {
 const client = makeStubClient({
  "/api/v1/auth/me": { id: "admin-1", email: "admin@example.com", is_platform_operator: true, operator_role: "admin", memberships: [] },
  "/api/v1/ops/customer-accounts": { accounts: [account], total: 1 },
  "/api/v1/ops/customer-accounts/customer-1": { id: account.id, email: account.email, identifiers: [{ key: "email:hash", kind: "email", label: account.email }, { key: "person:hash", kind: "person", label: "Didit verified identity: Customer" }], delete_workspaces: [{ id: "org-1", name: "Customer workspace" }], blockers },
  "/api/v1/ops/customer-accounts/customer-1/delete": {},
  "/api/v1/ops/customer-accounts/customer-1/blacklist": {},
 });
 renderWithProviders(<CustomerAccountsTab />, client);
 return client;
}
describe("All account administration", () => {
 it("lists unfinished signups and deletes with one confirmation", async () => {
  const client = setup();
  expect(await screen.findByText("Email not confirmed")).toBeInTheDocument();
  expect(screen.getByText(/individual · draft/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Manage account" }));
  await userEvent.click(await screen.findByRole("button", { name: "Delete account" }));
  // Nothing is sent until the operator confirms.
  expect(client.calls.some(c => c.path.endsWith("/delete"))).toBe(false);
  await userEvent.click(screen.getByRole("button", { name: "Yes, delete it" }));
  await waitFor(() => {
   const sent = client.calls.find(c => c.path.endsWith("/delete"))?.init.json as { confirmation: string; reason: string } | undefined;
   expect(sent?.confirmation).toBe(account.email);
   expect(sent?.reason.trim()).not.toBe("");
  });
 });
 it("cancelling the confirmation deletes nothing", async () => {
  const client = setup();
  await userEvent.click(await screen.findByRole("button", { name: "Manage account" }));
  await userEvent.click(await screen.findByRole("button", { name: "Delete account" }));
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("button", { name: "Yes, delete it" })).not.toBeInTheDocument();
  expect(client.calls.some(c => c.path.endsWith("/delete"))).toBe(false);
 });
 it("sends only the selected identity blacklist entries", async () => {
  const client = setup();
  await userEvent.click(await screen.findByRole("button", { name: "Manage account" }));
  await userEvent.click(await screen.findByRole("checkbox", { name: /Didit verified identity/ }));
  await userEvent.type(screen.getByLabelText("Account action reason"), "Fraud review");
  await userEvent.click(screen.getByRole("button", { name: "Block selected" }));
  await waitFor(() => expect(client.calls.find(c => c.path.endsWith("/blacklist"))?.init.json).toMatchObject({ identifiers: ["person:hash"] }));
 });
 it("explains active-resource blockers without showing a deletion action", async () => {
  setup(["Release phone numbers first"]);
  await userEvent.click(await screen.findByRole("button", { name: "Manage account" }));
  expect(await screen.findByText("Release phone numbers first")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Delete account" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Back to all accounts/ })).toBeEnabled();
 });
});
