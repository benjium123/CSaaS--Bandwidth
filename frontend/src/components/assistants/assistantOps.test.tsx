import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VoicePreviewButton } from "./VoicePreviewButton";
import { CallMePanel } from "./CallMePanel";
import {
  AssistantAnalyticsPanel,
  AssistantAnalyticsStrip,
} from "./AssistantAnalytics";
import { makeStubClient, renderWithProviders } from "@/test/harness";

beforeEach(() => {
  vi.stubGlobal(
    "URL",
    Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:x"),
      revokeObjectURL: vi.fn(),
    }),
  );
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});

const analyticsFixture = {
  calls: 3,
  minutes: 12.6,
  answer_rate: 0.9,
  handoff_rate: 0.25,
  booked: 1,
  avg_duration_seconds: 65,
  cost_micros: null,
};

describe("VoicePreviewButton", () => {
  it("is disabled and asks for a voice id when the voice id is blank", async () => {
    const client = makeStubClient({});
    renderWithProviders(
      <VoicePreviewButton ttsProvider="elevenlabs" voiceId="" />,
      client,
    );

    const button = screen.getByRole("button", { name: "Preview voice" });
    expect(button).toBeDisabled();
    expect(screen.getByText("Add a voice id first.")).toBeInTheDocument();
  });

  it("posts the preview body to the preview path", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) => {
        return new Response(new Blob(["x"], { type: "audio/mpeg" }), {
          status: 200,
        });
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = makeStubClient({});
    renderWithProviders(
      <VoicePreviewButton ttsProvider="elevenlabs" voiceId="v1" />,
      client,
    );

    await userEvent.click(screen.getByRole("button", { name: "Preview voice" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as [
      string,
      RequestInit & { body?: string },
    ];
    expect(url).toBe("/api/v1/agent/voices/preview");
    const body = JSON.parse(init.body ?? "{}");
    expect(body).toMatchObject({ tts_provider: "elevenlabs", voice_id: "v1" });
    expect(body.text).toBeDefined();
  });

  it("renders audio and plays it after the preview resolves", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(new Blob(["x"], { type: "audio/mpeg" }), {
          status: 200,
        });
      }),
    );

    const client = makeStubClient({});
    renderWithProviders(
      <VoicePreviewButton ttsProvider="elevenlabs" voiceId="v1" />,
      client,
    );

    await userEvent.click(screen.getByRole("button", { name: "Preview voice" }));

    await waitFor(() => expect(document.querySelector("audio")).not.toBeNull());
    expect(document.querySelector("audio")?.getAttribute("src")).toBe("blob:x");
    await waitFor(() =>
      expect(vi.mocked(HTMLMediaElement.prototype.play)).toHaveBeenCalled(),
    );
  });

  it("renders the server error message in an alert on a 422", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(
          JSON.stringify({ detail: "That voice is not available." }),
          {
            status: 422,
            headers: { "Content-Type": "application/json" },
          },
        );
      }),
    );

    const client = makeStubClient({});
    renderWithProviders(
      <VoicePreviewButton ttsProvider="elevenlabs" voiceId="v1" />,
      client,
    );

    await userEvent.click(screen.getByRole("button", { name: "Preview voice" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/That voice is not available/i);
  });

  it("shows Playing… and disables the button while the preview is in flight", async () => {
    let resolveFetch!: (value: Response) => void;
    const fetchMock = vi.fn(() => {
      return new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = makeStubClient({});
    renderWithProviders(
      <VoicePreviewButton ttsProvider="elevenlabs" voiceId="v1" />,
      client,
    );

    await userEvent.click(screen.getByRole("button", { name: "Preview voice" }));

    const button = await screen.findByRole("button", {
      name: "Preview voice",
    });
    expect(button).toHaveTextContent("Playing…");
    expect(button).toBeDisabled();

    resolveFetch(
      new Response(new Blob(["x"], { type: "audio/mpeg" }), {
        status: 200,
      }),
    );
    await waitFor(() =>
      expect(vi.mocked(HTMLMediaElement.prototype.play)).toHaveBeenCalled(),
    );
  });
});

describe("CallMePanel", () => {
  it("asks you to save first when the assistant is new", async () => {
    const client = makeStubClient({});
    renderWithProviders(<CallMePanel assistantId={null} />, client);

    expect(screen.getByRole("button", { name: "Call me" })).toBeDisabled();
    expect(screen.getByText("Save this assistant first.")).toBeInTheDocument();
  });

  it("stays disabled for a junk phone number", async () => {
    const client = makeStubClient({});
    renderWithProviders(
      <CallMePanel assistantId="a1" defaultPhone="123" />,
      client,
    );

    expect(screen.getByRole("button", { name: "Call me" })).toBeDisabled();
    expect(
      screen.queryByText("Save this assistant first."),
    ).not.toBeInTheDocument();
  });

  it("normalises a typed phone number into to_e164", async () => {
    const client = makeStubClient({
      "/api/v1/agent/profiles/a1/call-me": (
        _path: string,
        init: RequestInit & { json?: unknown },
      ) => {
        if ((init.method ?? "GET") === "POST") {
          return { call_id: "c1", status: "queued" };
        }
        return undefined;
      },
    });

    renderWithProviders(
      <CallMePanel assistantId="a1" defaultPhone="(214) 555-0100" />,
      client,
    );

    expect(screen.getByLabelText("Your phone number")).toHaveValue(
      "(214) 555-0100",
    );
    await userEvent.click(screen.getByRole("button", { name: "Call me" }));

    await waitFor(() => {
      const call = client.calls.find(
        (c) =>
          c.path === "/api/v1/agent/profiles/a1/call-me" &&
          (c.init.method ?? "GET") === "POST",
      );
      expect(call).toBeDefined();
      expect(call?.init.json).toEqual({ to_e164: "+12145550100" });
    });
  });

  it("shows success after the call is queued", async () => {
    const client = makeStubClient({
      "/api/v1/agent/profiles/a1/call-me": (
        _path: string,
        init: RequestInit & { json?: unknown },
      ) => {
        if ((init.method ?? "GET") === "POST") {
          return { call_id: "c1", status: "queued" };
        }
        return undefined;
      },
    });

    renderWithProviders(
      <CallMePanel assistantId="a1" defaultPhone="(214) 555-0100" />,
      client,
    );

    await userEvent.click(screen.getByRole("button", { name: "Call me" }));

    expect(
      await screen.findByText("Calling you now — pick up."),
    ).toBeInTheDocument();
  });

  it("renders the failure message", async () => {
    const client = makeStubClient({
      "/api/v1/agent/profiles/a1/call-me": (
        _path: string,
        init: RequestInit & { json?: unknown },
      ) => {
        if ((init.method ?? "GET") === "POST") {
          throw new Error("We could not place the call.");
        }
        return undefined;
      },
    });

    renderWithProviders(
      <CallMePanel assistantId="a1" defaultPhone="(214) 555-0100" />,
      client,
    );

    await userEvent.click(screen.getByRole("button", { name: "Call me" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("We could not place the call.");
  });
});

describe("AssistantAnalytics", () => {
  it("renders the six core tiles with formatted values", async () => {
    const client = makeStubClient({
      "/api/v1/analytics/assistant": analyticsFixture,
    });
    renderWithProviders(<AssistantAnalyticsStrip days={30} />, client);

    const list = await screen.findByRole("list");
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(6);

    expect(within(list).getByText("Calls")).toBeInTheDocument();
    expect(within(list).getByText("3")).toBeInTheDocument();
    expect(within(list).getByText("Minutes")).toBeInTheDocument();
    expect(within(list).getByText("13")).toBeInTheDocument();
    expect(within(list).getByText("Answered")).toBeInTheDocument();
    expect(within(list).getByText("90%")).toBeInTheDocument();
    expect(within(list).getByText("Passed to a person")).toBeInTheDocument();
    expect(within(list).getByText("25%")).toBeInTheDocument();
    expect(within(list).getByText("Booked")).toBeInTheDocument();
    expect(within(list).getByText("1")).toBeInTheDocument();
    expect(within(list).getByText("Average call")).toBeInTheDocument();
    expect(within(list).getByText("1m 5s")).toBeInTheDocument();
  });

  it("omits Cost when cost_micros is null", async () => {
    const client = makeStubClient({
      "/api/v1/analytics/assistant": analyticsFixture,
    });
    renderWithProviders(<AssistantAnalyticsStrip days={30} />, client);

    await screen.findByText("Calls");
    expect(screen.queryByText("Cost")).not.toBeInTheDocument();
  });

  it("renders cost micros as dollars when present", async () => {
    const client = makeStubClient({
      "/api/v1/analytics/assistant": {
        ...analyticsFixture,
        cost_micros: 4_250_000,
      },
    });
    renderWithProviders(<AssistantAnalyticsStrip days={30} />, client);

    expect(await screen.findByText("Cost")).toBeInTheDocument();
    expect(screen.getByText("$4.25")).toBeInTheDocument();
  });

  it("shows the empty state when calls is zero", async () => {
    const client = makeStubClient({
      "/api/v1/analytics/assistant": {
        calls: 0,
        minutes: 0,
        answer_rate: null,
        handoff_rate: null,
        booked: 0,
        avg_duration_seconds: 0,
        cost_micros: null,
      },
    });
    renderWithProviders(<AssistantAnalyticsStrip days={30} />, client);

    expect(await screen.findByText("No assistant calls yet")).toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(screen.queryByText("Calls")).not.toBeInTheDocument();
  });

  it("renders errors with alert role", async () => {
    const client = makeStubClient({
      "/api/v1/analytics/assistant": (
        _path: string,
        _init: RequestInit & { json?: unknown },
      ) => {
        throw new Error("Activity is unavailable.");
      },
    });
    renderWithProviders(<AssistantAnalyticsStrip days={30} />, client);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Activity is unavailable.");
  });

  it("refetches with a 7-day inclusive window when 7d is pressed", async () => {
    const client = makeStubClient({
      "/api/v1/analytics/assistant": (
        _path: string,
        _init: RequestInit & { json?: unknown },
      ) => analyticsFixture,
    });
    renderWithProviders(<AssistantAnalyticsPanel />, client);

    await screen.findByText("Calls");
    await userEvent.click(screen.getByRole("button", { name: "7d" }));

    await waitFor(() => {
      const paths = client.calls
        .filter((call) => call.path.startsWith("/api/v1/analytics/assistant"))
        .map((call) => call.path);
      expect(paths.length).toBeGreaterThanOrEqual(2);

      const url = new URL(paths[paths.length - 1], "http://localhost");
      const from = url.searchParams.get("from");
      const to = url.searchParams.get("to");
      if (!from || !to) throw new Error("Missing from/to in analytics request");

      const inclusiveDays =
        Math.round(
          (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) /
            86400000,
        ) + 1;
      expect(inclusiveDays).toBe(7);
    });
  });
});
