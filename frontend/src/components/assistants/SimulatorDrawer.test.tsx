import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SimulatorDrawer } from "./SimulatorDrawer";
import { makeStubClient, renderWithProviders } from "@/test/harness";

describe("SimulatorDrawer", () => {
  it("invites you to say something before anything is sent", async () => {
    const client = makeStubClient({});

    renderWithProviders(
      <SimulatorDrawer
        open
        onClose={() => {}}
        assistantId="a1"
        assistantName="Atlas"
      />,
      client,
    );

    expect(
      await screen.findByText("Say something and see how it answers."),
    ).toBeInTheDocument();
  });

  it("sends the whole conversation and shows the reply", async () => {
    const posts: unknown[] = [];
    const client = makeStubClient({
      "/api/v1/agent/profiles/a1/simulate": (
        _path: string,
        init: RequestInit & { json?: unknown },
      ) => {
        if ((init.method ?? "GET") === "POST") {
          posts.push(init.json);
          return { reply: "Hey there!", tokens_in: 2, tokens_out: 3, kb_hits: [] };
        }
        return undefined;
      },
    });

    renderWithProviders(
      <SimulatorDrawer
        open
        onClose={() => {}}
        assistantId="a1"
        assistantName="Atlas"
      />,
      client,
    );

    const input = await screen.findByLabelText("Your message");
    await userEvent.type(input, "Hi");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("Hey there!")).toBeInTheDocument();
    expect(posts[0]).toEqual({ messages: [{ role: "user", content: "Hi" }] });

    await userEvent.type(input, "Again");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(posts.length).toBe(2));
    expect(posts[1]).toEqual({
      messages: [
        { role: "user", content: "Hi" },
        { role: "assistant", content: "Hey there!" },
        { role: "user", content: "Again" },
      ],
    });
  });

  it("shows how much it used", async () => {
    const client = makeStubClient({
      "/api/v1/agent/profiles/a1/simulate": () => ({
        reply: "Hi.",
        tokens_in: 12,
        tokens_out: 34,
        kb_hits: [],
      }),
    });

    renderWithProviders(
      <SimulatorDrawer
        open
        onClose={() => {}}
        assistantId="a1"
        assistantName="Atlas"
      />,
      client,
    );

    await userEvent.type(await screen.findByLabelText("Your message"), "Hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("Tokens: 12 in, 34 out.")).toBeInTheDocument();
  });

  it("names the knowledge it used", async () => {
    const client = makeStubClient({
      "/api/v1/agent/profiles/a1/simulate": () => ({
        reply: "Here you go.",
        tokens_in: 1,
        tokens_out: 1,
        kb_hits: [
          { document_id: "d1", title: "Hours", snippet: "We open at nine." },
        ],
      }),
    });

    renderWithProviders(
      <SimulatorDrawer
        open
        onClose={() => {}}
        assistantId="a1"
        assistantName="Atlas"
      />,
      client,
    );

    await userEvent.type(
      await screen.findByLabelText("Your message"),
      "When are you open?",
    );
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    const list = await screen.findByRole("list", { name: "Knowledge used" });
    expect(await within(list).findByText("Hours")).toBeInTheDocument();
  });

  it("keeps your words when the answer fails", async () => {
    const client = makeStubClient({
      "/api/v1/agent/profiles/a1/simulate": () => {
        throw new Error("The simulator is out of order.");
      },
    });

    renderWithProviders(
      <SimulatorDrawer
        open
        onClose={() => {}}
        assistantId="a1"
        assistantName="Atlas"
      />,
      client,
    );

    await userEvent.type(await screen.findByLabelText("Your message"), "Hello?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("Hello?")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("asks you to save first when the assistant is new", async () => {
    const client = makeStubClient({});

    renderWithProviders(
      <SimulatorDrawer
        open
        onClose={() => {}}
        assistantId={null}
        assistantName="Untitled"
      />,
      client,
    );

    expect(await screen.findByText("Save your assistant first")).toBeInTheDocument();
    expect(screen.queryByLabelText("Your message")).not.toBeInTheDocument();
  });
});
