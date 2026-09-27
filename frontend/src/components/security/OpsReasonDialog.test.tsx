import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OpsReasonDialog } from "@/components/security/OpsReasonDialog";

const api: {
  onReasonRequired?: (details: { message: string }) => Promise<string | null>;
} = {};

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ api, me: null }),
}));

afterEach(() => {
  cleanup();
  api.onReasonRequired = undefined;
});

describe("OpsReasonDialog", () => {
  it("renders nothing and stays subscribed until the API asks for a reason", () => {
    render(<OpsReasonDialog />);

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(typeof api.onReasonRequired).toBe("function");
  });

  it("resolves the trimmed reason when the operator hits Continue", async () => {
    const user = userEvent.setup();
    render(<OpsReasonDialog />);

    let promise: Promise<string | null> | undefined;
    act(() => {
      promise = api.onReasonRequired!({ message: "Say why" });
    });

    expect(screen.queryByRole("dialog")).not.toBeNull();
    expect(screen.getByText("Say why")).toBeTruthy();

    const reason = screen.getByLabelText("Reason");
    const continueButton = screen.getByRole("button", {
      name: "Continue",
    }) as HTMLButtonElement;

    await user.type(reason, "abc");
    expect(continueButton.disabled).toBe(true);

    await user.clear(reason);
    await user.type(reason, "owner asked");
    expect(continueButton.disabled).toBe(false);

    await user.click(continueButton);
    await expect(promise).resolves.toBe("owner asked");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("resolves null when the operator cancels", async () => {
    const user = userEvent.setup();
    render(<OpsReasonDialog />);

    let promise: Promise<string | null> | undefined;
    act(() => {
      promise = api.onReasonRequired!({ message: "Say why" });
    });

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await expect(promise).resolves.toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("resolves null and unsubscribes when unmounted while pending", async () => {
    const { unmount } = render(<OpsReasonDialog />);

    let promise: Promise<string | null> | undefined;
    act(() => {
      promise = api.onReasonRequired!({ message: "Say why" });
    });

    unmount();
    expect(api.onReasonRequired).toBeUndefined();
    await expect(promise).resolves.toBeNull();
  });
});
