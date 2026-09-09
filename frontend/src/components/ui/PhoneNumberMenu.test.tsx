import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PhoneNumberMenu } from "./PhoneNumberMenu";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { formatPhone } from "@/lib/format";

const { dialMock } = vi.hoisted(() => ({
  dialMock: vi.fn(),
}));

// Do NOT mount the real SoftphoneProvider - it opens a websocket. This fake only
// supplies the one method PhoneNumberMenu touches.
vi.mock("@/softphone/SoftphoneProvider", () => ({
  useSoftphone: () => ({ dial: dialMock }),
}));

const ME = {
  id: "u1",
  email: "u@example.com",
  full_name: "U Ser",
  memberships: [],
};

function renderMenu(props: Partial<React.ComponentProps<typeof PhoneNumberMenu>> = {}) {
  const client = makeStubClient({ "/api/v1/auth/me": ME });
  return renderWithProviders(<PhoneNumberMenu e164="+14694617576" {...props} />, client);
}

beforeEach(() => {
  dialMock.mockClear();
});

describe("PhoneNumberMenu", () => {
  it("renders the formatted number", () => {
    renderMenu();

    const trigger = screen.getByRole("button", {
      name: `Actions for ${formatPhone("+14694617576")}`,
    });
    expect(trigger).toHaveTextContent(formatPhone("+14694617576"));
  });

  it("opens on click and exposes Text + Call menu items", async () => {
    renderMenu({ onText: vi.fn() });

    await userEvent.click(screen.getByRole("button", { name: /Actions for/ }));
    const menu = await screen.findByRole("menu", { name: "Phone number actions" });

    expect(within(menu).getByRole("menuitem", { name: "Text" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Call" })).toBeInTheDocument();
  });

  it('"Call" calls the softphone dial with (e164, fromE164)', async () => {
    renderMenu({ fromE164: "+12145550111" });

    await userEvent.click(screen.getByRole("button", { name: /Actions for/ }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Call" }));

    await waitFor(() =>
      expect(dialMock).toHaveBeenCalledWith("+14694617576", "+12145550111"),
    );
  });

  it('"Text" calls onText', async () => {
    const onText = vi.fn();
    renderMenu({ onText });

    await userEvent.click(screen.getByRole("button", { name: /Actions for/ }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Text" }));

    expect(onText).toHaveBeenCalledWith("+14694617576");
  });

  it("Escape closes and refocuses the trigger", async () => {
    renderMenu({ onText: vi.fn() });

    const trigger = screen.getByRole("button", { name: /Actions for/ });
    await userEvent.click(trigger);
    expect(
      screen.getByRole("menu", { name: "Phone number actions" }),
    ).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");

    expect(
      screen.queryByRole("menu", { name: "Phone number actions" }),
    ).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("with onText omitted no Text item exists", async () => {
    renderMenu();

    await userEvent.click(screen.getByRole("button", { name: /Actions for/ }));

    expect(
      screen.queryByRole("menuitem", { name: "Text" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Call" })).toBeInTheDocument();
  });
});
