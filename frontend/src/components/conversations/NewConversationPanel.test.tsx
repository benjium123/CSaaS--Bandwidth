import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/api/client";
import { NewConversationPanel } from "./NewConversationPanel";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const FROM_OPTIONS = [
  { e164: "+14694617576", label: "Sales" },
  { e164: "+12145550111", label: "Support" },
];

function renderPanel(
  kind: "message" | "call",
  overrides: {
    onSendMessage?: (vars: {
      from: string;
      to: string;
      body: string;
      allowReassign: boolean;
    }) => Promise<void>;
    onCall?: (vars: { from: string; to: string }) => Promise<void>;
    onCancel?: () => void;
    contactsStub?: unknown;
  } = {},
) {
  const client = makeStubClient({
    "/api/v1/contacts": overrides.contactsStub ?? [
      { id: "c1", display_name: "Ada Lovelace", phones: [{ e164: "+19725550199" }] },
    ],
  });
  const onSendMessage = overrides.onSendMessage ?? vi.fn().mockResolvedValue(undefined);
  const onCall = overrides.onCall ?? vi.fn().mockResolvedValue(undefined);
  const onCancel = overrides.onCancel ?? vi.fn();
  renderWithProviders(
    <NewConversationPanel
      kind={kind}
      fromOptions={FROM_OPTIONS}
      onCancel={onCancel}
      onSendMessage={onSendMessage}
      onCall={onCall}
    />,
    client,
  );
  return { client, onSendMessage, onCall, onCancel };
}

describe("NewConversationPanel", () => {
  it("renders the From options and keeps Send disabled until To and body are filled", async () => {
    renderPanel("message");

    expect(screen.getByRole("heading", { name: "New text message" })).toBeInTheDocument();
    const fromSelect = screen.getByLabelText("From") as HTMLSelectElement;
    expect(fromSelect.value).toBe("+14694617576");
    expect(
      screen.getByRole("option", { name: /Support/ }),
    ).toBeInTheDocument();

    const sendButton = screen.getByRole("button", { name: "Send" });
    expect(sendButton).toBeDisabled();

    await userEvent.type(screen.getByLabelText("To"), "9725550100");
    expect(sendButton).toBeDisabled(); // body still empty

    await userEvent.type(screen.getByLabelText("Message"), "hello there");
    expect(sendButton).toBeEnabled();
  });

  it("normalizes a raw 10-digit number and sends via onSendMessage with the resolved E.164", async () => {
    const { onSendMessage } = renderPanel("message");

    await userEvent.type(screen.getByLabelText("To"), "9725550100");
    await userEvent.type(screen.getByLabelText("Message"), "hi");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(onSendMessage).toHaveBeenCalledWith({
        from: "+14694617576",
        to: "+19725550100",
        body: "hi",
        allowReassign: false,
      }),
    );
  });

  it("picking a contact suggestion resolves To without needing a normalizable raw number", async () => {
    const { onSendMessage } = renderPanel("message");

    await userEvent.type(screen.getByLabelText("To"), "Ada");
    const option = await screen.findByRole("option", { name: /Ada Lovelace/ });
    await userEvent.click(option);
    await userEvent.type(screen.getByLabelText("Message"), "hi Ada");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(onSendMessage).toHaveBeenCalledWith({
        from: "+14694617576",
        to: "+19725550199",
        body: "hi Ada",
        allowReassign: false,
      }),
    );
  });

  it("kind=call has no message field and submits via onCall", async () => {
    const { onCall } = renderPanel("call");

    expect(screen.queryByLabelText("Message")).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("To"), "9725550100");
    const callButton = screen.getByRole("button", { name: "Call" });
    expect(callButton).toBeEnabled();
    await userEvent.click(callButton);

    await waitFor(() =>
      expect(onCall).toHaveBeenCalledWith({ from: "+14694617576", to: "+19725550100" }),
    );
  });

  it("shows an inline error when the send fails", async () => {
    const onSendMessage = vi.fn().mockRejectedValue(new Error("sticky sender unavailable"));
    renderPanel("message", { onSendMessage });

    await userEvent.type(screen.getByLabelText("To"), "9725550100");
    await userEvent.type(screen.getByLabelText("Message"), "hi");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("sticky sender unavailable");
  });

  it("calls onCancel from the close button", async () => {
    const { onCancel } = renderPanel("message");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
  });

  // Item 3: mirrors Composer.tsx's sticky_sender_unavailable confirm.
  it("shows a Send anyway confirm on sticky_sender_unavailable and resubmits with allowReassign", async () => {
    const onSendMessage = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(422, "sticky_sender_unavailable", "number retired"))
      .mockResolvedValueOnce(undefined);
    renderPanel("message", { onSendMessage });

    await userEvent.type(screen.getByLabelText("To"), "9725550100");
    await userEvent.type(screen.getByLabelText("Message"), "hi");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This number was retired. Send from a new number?");

    await userEvent.click(within(alert).getByRole("button", { name: "Send anyway" }));

    await waitFor(() => expect(onSendMessage).toHaveBeenCalledTimes(2));
    expect(onSendMessage).toHaveBeenLastCalledWith({
      from: "+14694617576",
      to: "+19725550100",
      body: "hi",
      allowReassign: true,
    });
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("dismisses the sticky-sender confirm via its own Cancel without resubmitting", async () => {
    const onSendMessage = vi
      .fn()
      .mockRejectedValue(new ApiError(422, "sticky_sender_unavailable", "number retired"));
    renderPanel("message", { onSendMessage });

    await userEvent.type(screen.getByLabelText("To"), "9725550100");
    await userEvent.type(screen.getByLabelText("Message"), "hi");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    const alert = await screen.findByRole("alert");
    await userEvent.click(within(alert).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(onSendMessage).toHaveBeenCalledTimes(1);
  });

  // Item 4: no contacts fetch until the query is at least 2 characters.
  it("does not search contacts until the query is at least 2 characters", async () => {
    const contactsStub = vi
      .fn()
      .mockReturnValue([
        { id: "c1", display_name: "Ada Lovelace", phones: [{ e164: "+19725550199" }] },
      ]);
    renderPanel("message", { contactsStub });

    await userEvent.type(screen.getByLabelText("To"), "A");
    await new Promise((resolve) => setTimeout(resolve, 350)); // past the 300ms debounce
    expect(contactsStub).not.toHaveBeenCalled();

    await userEvent.type(screen.getByLabelText("To"), "d");
    await waitFor(() => expect(contactsStub).toHaveBeenCalled());
  });

  // Item 5: combobox semantics - role/aria wiring and keyboard navigation.
  it("supports combobox keyboard navigation and exposes role=option directly on listbox children", async () => {
    const contactsStub = [
      { id: "c1", display_name: "Ada Lovelace", phones: [{ e164: "+19725550199" }] },
      { id: "c2", display_name: "Ada Byron", phones: [{ e164: "+19725550200" }] },
    ];
    renderPanel("message", { contactsStub });

    const toInput = screen.getByLabelText("To");
    expect(toInput).toHaveAttribute("role", "combobox");
    expect(toInput).toHaveAttribute("aria-expanded", "false");

    await userEvent.type(toInput, "Ada");
    // Scoped to the listbox - the "From" <select>'s native <option> elements also carry
    // an implicit role="option" and would otherwise pollute an unscoped query.
    const listbox = await screen.findByRole("listbox");
    const options = within(listbox).getAllByRole("option");
    expect(options).toHaveLength(2);
    // role=option sits directly on the listbox's children - no extra wrapper element.
    expect(options[0].tagName).toBe("LI");
    expect(toInput).toHaveAttribute("aria-expanded", "true");
    expect(toInput).toHaveAttribute("aria-controls", listbox.id);

    await userEvent.keyboard("{ArrowDown}");
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    expect(toInput).toHaveAttribute("aria-activedescendant", options[0].id);

    await userEvent.keyboard("{ArrowDown}");
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    expect(toInput).toHaveAttribute("aria-activedescendant", options[1].id);

    await userEvent.keyboard("{Enter}");
    expect(toInput).toHaveValue("Ada Byron · (972) 555-0200");
    expect(toInput).toHaveAttribute("aria-expanded", "false");
  });
});
