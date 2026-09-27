import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StartCallDialog } from "./StartCallDialog";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const FROM_OPTIONS = [
  { e164: "+14694617576", label: "Dispo caller" },
  { e164: "+12145550111", label: "Support" },
];

function renderDialog(overrides: {
  onCall?: (vars: { from: string; to: string }) => Promise<void>;
  onCancel?: () => void;
  contactsStub?: unknown;
} = {}) {
  const client = makeStubClient({
    "/api/v1/contacts": overrides.contactsStub ?? [
      { id: "c1", display_name: "Ada Lovelace", phones: [{ e164: "+19725550199" }] },
    ],
  });
  const onCall = overrides.onCall ?? vi.fn().mockResolvedValue(undefined);
  const onCancel = overrides.onCancel ?? vi.fn();
  renderWithProviders(
    <StartCallDialog fromOptions={FROM_OPTIONS} onCancel={onCancel} onCall={onCall} />,
    client,
  );
  return { client, onCall, onCancel };
}

describe("StartCallDialog", () => {
  it("renders as a dialog with the line picker and an autofocused number field", () => {
    renderDialog();

    const dialog = screen.getByRole("dialog", { name: "Start a call" });
    expect(dialog).toBeInTheDocument();
    expect(screen.getByText("Make the call from")).toBeInTheDocument();

    const fromSelect = screen.getByLabelText("Call from") as HTMLSelectElement;
    expect(fromSelect.value).toBe("+14694617576");
    expect(screen.getByRole("option", { name: /Support/ })).toBeInTheDocument();

    const input = screen.getByLabelText("Enter a name or phone number") as HTMLInputElement;
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute("placeholder", "Enter a name or phone number…");
  });

  it("picks a different line and calls onCall with that line once a number is typed", async () => {
    const { onCall } = renderDialog();

    await userEvent.selectOptions(screen.getByLabelText("Call from"), "+12145550111");
    await userEvent.type(
      screen.getByLabelText("Enter a name or phone number"),
      "9725550100",
    );
    await userEvent.keyboard("{Enter}");

    await waitFor(() =>
      expect(onCall).toHaveBeenCalledWith({ from: "+12145550111", to: "+19725550100" }),
    );
  });

  it("clicking a contact suggestion calls onCall with the contact's E.164 directly", async () => {
    const { onCall } = renderDialog();

    await userEvent.type(screen.getByLabelText("Enter a name or phone number"), "Ada");
    const option = await screen.findByRole("option", { name: /Ada Lovelace/ });
    await userEvent.click(option);

    await waitFor(() =>
      expect(onCall).toHaveBeenCalledWith({ from: "+14694617576", to: "+19725550199" }),
    );
  });

  it("closes on Escape", async () => {
    const { onCancel } = renderDialog();

    await userEvent.keyboard("{Escape}");

    expect(onCancel).toHaveBeenCalled();
  });
});
