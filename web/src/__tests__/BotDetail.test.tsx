/**
 * Vitest component tests for the "Stop bot" control on BotDetail.tsx.
 *
 * Mocks the API client module directly (frontend convention — distinct from
 * the Python backend's "never mock" testing philosophy).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import BotDetail from "../pages/BotDetail.tsx";
import { api, controlApi } from "../lib/api.ts";
import type { BotDetail as BotDetailResponse } from "../lib/api.ts";

vi.mock("../lib/api.ts", () => ({
  api: {
    bot: vi.fn(),
  },
  controlApi: {
    stopBot: vi.fn(),
  },
}));

const MOCK_BOT: BotDetailResponse = {
  bot_id: "test-bot-1",
  strategy: "momentum_v1",
  market_id: "market-abc",
  mode: "paper",
  snapshot_at: "2026-05-09T12:00:00Z",
  version: 3,
  state: { position: "0" },
  recent_audit: [],
};

function renderBotDetail() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/bots/test-bot-1"]}>
        <Routes>
          <Route path="/bots/:botId" element={<BotDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("BotDetail — Stop bot control", () => {
  beforeEach(() => {
    vi.mocked(api.bot).mockResolvedValue(MOCK_BOT);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the Stop bot button after loading", async () => {
    renderBotDetail();
    const button = await screen.findByRole("button", { name: /stop bot/i });
    expect(button).toBeTruthy();
  });

  it("does not call controlApi.stopBot when confirmation is cancelled", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    renderBotDetail();

    const button = await screen.findByRole("button", { name: /stop bot/i });
    await user.click(button);

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(controlApi.stopBot).not.toHaveBeenCalled();
  });

  it("calls controlApi.stopBot with the bot id on confirm and shows stopped message", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(controlApi.stopBot).mockResolvedValue({
      bot_id: "test-bot-1",
      stopped: true,
    });
    const user = userEvent.setup();
    renderBotDetail();

    const button = await screen.findByRole("button", { name: /stop bot/i });
    await user.click(button);

    expect(controlApi.stopBot).toHaveBeenCalledWith("test-bot-1");
    const message = await screen.findByText(/stopped\./i);
    expect(message).toBeTruthy();
  });

  it("shows a distinct not-running message when stopped is false", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(controlApi.stopBot).mockResolvedValue({
      bot_id: "test-bot-1",
      stopped: false,
    });
    const user = userEvent.setup();
    renderBotDetail();

    const button = await screen.findByRole("button", { name: /stop bot/i });
    await user.click(button);

    const message = await screen.findByText(/was not running/i);
    expect(message).toBeTruthy();
    expect(screen.queryByText(/^bot test-bot-1 stopped\.$/i)).toBeNull();
  });

  it("shows an error message on network/server failure without crashing", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(controlApi.stopBot).mockRejectedValue(new Error("API returned 500"));
    const user = userEvent.setup();
    renderBotDetail();

    const button = await screen.findByRole("button", { name: /stop bot/i });
    await user.click(button);

    const message = await screen.findByText(/API returned 500/i);
    expect(message).toBeTruthy();
  });

  it("disables the button while the mutation is in-flight", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let resolveFn: (v: { bot_id: string; stopped: boolean }) => void = () => {};
    vi.mocked(controlApi.stopBot).mockReturnValue(
      new Promise((resolve) => {
        resolveFn = resolve;
      }),
    );
    const user = userEvent.setup();
    renderBotDetail();

    const button = await screen.findByRole("button", { name: /stop bot/i });
    await user.click(button);

    await waitFor(() => expect(button.hasAttribute("disabled")).toBe(true));

    resolveFn({ bot_id: "test-bot-1", stopped: true });
    await waitFor(() => expect(button.hasAttribute("disabled")).toBe(false));
  });
});
