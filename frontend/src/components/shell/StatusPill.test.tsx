import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useAuth } from "@/auth/AuthContext";
import { dndTimeLeft, nextEightAm } from "@/api/callPrefs";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { StatusPill } from "./StatusPill";

const TEAMMATE = { user_id: "u2", name: "Bob Jones" };

/** The me payload is loaded from the auth endpoint; `permissions` is what the pill gates on. */
function makeMe(permissions: string[]) {
  return {
    id: "u1",
    email: "ada@example.com",
    full_name: "Ada Lovelace",
    permissions,
    memberships: [
      {
        org_id: "org-1",
        org_name: "Org",
        org_slug: "org",
        role_name: "owner",
        permissions,
      },
    ],
  };
}

const ME_WITH_PLACE = makeMe(["calls:place"]);
const ME_WITHOUT_PLACE = makeMe([]);

const AVAILABLE = { dnd: false, dnd_until: null, forward_to: null, teammates: [TEAMMATE] };

function makeClient(me: unknown, prefs: Record<string, unknown> = AVAILABLE) {
  return makeStubClient({
    "/api/v1/auth/me": me,
    "/api/v1/me": me,
    "/api/v1/me/call-prefs": (_path: string, init: RequestInit & { json?: unknown }) => {
      if (init.method === "PUT") {
        return { ...(init.json as Record<string, unknown>) };
      }
      return prefs;
    },
  });
}

type StubClient = ReturnType<typeof makeClient>;

function putCalls(client: StubClient) {
  return client.calls.filter(
    (call) => call.path === "/api/v1/me/call-prefs" && call.init.method === "PUT",
  );
}

function MeProbe() {
  const { me } = useAuth();
  return <span>{me ? "loaded" : "pending"}</span>;
}

describe("StatusPill", () => {
  it("shows Status: Available once the prefs load", async () => {
    renderWithProviders(<StatusPill />, makeClient(ME_WITH_PLACE));
    expect(await screen.findByRole("button", { name: "Status: Available" })).toBeInTheDocument();
  });

  it("saves DND for 30 minutes about 30 minutes ahead", async () => {
    const user = userEvent.setup();
    const client = makeClient(ME_WITH_PLACE);
    renderWithProviders(<StatusPill />, client);

    await user.click(await screen.findByRole("button", { name: "Status: Available" }));
    await user.click(screen.getByRole("menuitemradio", { name: "Do not disturb for 30 minutes" }));

    await waitFor(() => expect(putCalls(client)).toHaveLength(1));
    const body = putCalls(client)[0].init.json as { dnd: boolean; dnd_until: string | null };
    expect(body.dnd).toBe(true);
    const delta = new Date(body.dnd_until as string).getTime() - Date.now();
    expect(Math.abs(delta - 30 * 60_000)).toBeLessThan(60_000);
  });

  it("turning DND off PUTs the explicit all-clear body", async () => {
    const user = userEvent.setup();
    const client = makeClient(ME_WITH_PLACE, {
      dnd: true,
      dnd_until: new Date(Date.now() + 60 * 60_000).toISOString(),
      forward_to: "u2",
      teammates: [TEAMMATE],
    });
    renderWithProviders(<StatusPill />, client);

    await user.click(await screen.findByRole("button", { name: "Status: Do not disturb" }));
    await user.click(screen.getByRole("menuitemradio", { name: "Available" }));

    await waitFor(() => expect(putCalls(client)).toHaveLength(1));
    expect(putCalls(client)[0].init.json).toEqual({
      dnd: false,
      dnd_until: null,
      forward_to: null,
    });
  });

  it("shows the time left while DND has an end", async () => {
    const client = makeClient(ME_WITH_PLACE, {
      dnd: true,
      dnd_until: new Date(Date.now() + 58 * 60_000 + 30_000).toISOString(),
      forward_to: null,
      teammates: [],
    });
    renderWithProviders(<StatusPill />, client);

    const trigger = await screen.findByRole("button", { name: "Status: Do not disturb" });
    expect(trigger).toHaveTextContent("58m left");
  });

  it("shows the forward select only while DND, and saves the chosen teammate", async () => {
    const user = userEvent.setup();
    const client = makeClient(ME_WITH_PLACE);
    renderWithProviders(<StatusPill />, client);

    await user.click(await screen.findByRole("button", { name: "Status: Available" }));
    expect(screen.queryByLabelText("Send my calls to")).not.toBeInTheDocument();

    await user.click(screen.getByRole("menuitemradio", { name: "Do not disturb for 30 minutes" }));
    await waitFor(() => expect(putCalls(client)).toHaveLength(1));

    const select = await screen.findByLabelText("Send my calls to");
    await user.selectOptions(select, "u2");

    await waitFor(() => expect(putCalls(client)).toHaveLength(2));
    expect(putCalls(client)[1].init.json).toEqual(
      expect.objectContaining({ dnd: true, forward_to: "u2" }),
    );

    // Back to voicemail really clears the forward (null must not fall back to the old target).
    await user.selectOptions(await screen.findByLabelText("Send my calls to"), "");
    await waitFor(() => expect(putCalls(client)).toHaveLength(3));
    expect(putCalls(client)[2].init.json).toEqual(
      expect.objectContaining({ dnd: true, forward_to: null }),
    );
  });

  it("renders nothing without calls:place", async () => {
    renderWithProviders(
      <>
        <StatusPill />
        <MeProbe />
      </>,
      makeClient(ME_WITHOUT_PLACE),
    );

    await screen.findByText("loaded");
    expect(screen.queryByRole("button", { name: /Status:/ })).not.toBeInTheDocument();
  });
});

describe("dndTimeLeft", () => {
  const now = new Date("2024-01-01T12:00:00.000Z");

  it("is null for a missing or past end", () => {
    expect(dndTimeLeft(null, now)).toBeNull();
    expect(dndTimeLeft(new Date(now.getTime() - 1_000).toISOString(), now)).toBeNull();
  });

  it("counts under an hour in minutes", () => {
    expect(dndTimeLeft(new Date(now.getTime() + 58 * 60_000).toISOString(), now)).toBe("58m left");
  });

  it("omits the minutes when a whole number of hours remain", () => {
    expect(dndTimeLeft(new Date(now.getTime() + 2 * 60 * 60_000).toISOString(), now)).toBe("2h left");
  });

  it("counts hours and minutes together", () => {
    expect(dndTimeLeft(new Date(now.getTime() + 125 * 60_000).toISOString(), now)).toBe("2h 5m left");
  });

  it("is null once a day or more remain", () => {
    expect(dndTimeLeft(new Date(now.getTime() + 24 * 60 * 60_000).toISOString(), now)).toBeNull();
  });
});

describe("nextEightAm", () => {
  it("returns today's 08:00 for 07:59", () => {
    const result = nextEightAm(new Date(2024, 0, 15, 7, 59));
    expect([
      result.getFullYear(),
      result.getMonth(),
      result.getDate(),
      result.getHours(),
      result.getMinutes(),
    ]).toEqual([2024, 0, 15, 8, 0]);
  });

  it("returns tomorrow's 08:00 at exactly 08:00", () => {
    const result = nextEightAm(new Date(2024, 0, 15, 8, 0));
    expect([result.getMonth(), result.getDate(), result.getHours()]).toEqual([0, 16, 8]);
  });
});
