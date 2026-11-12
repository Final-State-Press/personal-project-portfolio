import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const state = vi.fn();
const failure = vi.fn();
const restoreLatest = vi.fn();
const dashboardGet = vi.fn();
const projectsList = vi.fn();

vi.mock("@/command-client", async () => {
  const actual = await vi.importActual<typeof import("@/command-client")>("@/command-client");
  return {
    ...actual,
    health: { state: () => state(), failure: () => failure() },
    backups: { restoreLatest: () => restoreLatest() },
    dashboard: { get: (...a: unknown[]) => dashboardGet(...a) },
    projects: { list: (...a: unknown[]) => projectsList(...a) },
  };
});

import { AppShell } from "./AppShell";
import { WorkspaceProvider } from "@/state/workspace";

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
}

function renderShell(client = makeClient()) {
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <WorkspaceProvider>{children}</WorkspaceProvider>
    </QueryClientProvider>
  );
  return { client, ...render(<AppShell />, { wrapper: Wrapper }) };
}

const SIDECAR_DOWN = { code: "SIDECAR_UNAVAILABLE", message: "request to sidecar failed" };

describe("AppShell when the backend is unavailable", () => {
  beforeEach(() => {
    state.mockReset();
    failure.mockReset();
    failure.mockResolvedValue(null);
    dashboardGet.mockReset();
    projectsList.mockReset();
    dashboardGet.mockResolvedValue(undefined);
    projectsList.mockResolvedValue({ projects: [] });
  });

  it("shows no banner while the backend is ready", async () => {
    state.mockResolvedValue("ready");
    renderShell();
    await waitFor(() => expect(screen.getByLabelText("Sidecar ready")).toBeInTheDocument());
    expect(screen.queryByText(/backend is unavailable/i)).not.toBeInTheDocument();
  });

  it("shows no banner while the backend is still starting", async () => {
    state.mockResolvedValue("starting");
    renderShell();
    await waitFor(() => expect(screen.getByLabelText("Sidecar starting")).toBeInTheDocument());
    expect(screen.queryByRole("alert", { name: /./ })).not.toBeInTheDocument();
    expect(screen.queryByText(/backend is unavailable/i)).not.toBeInTheDocument();
  });

  it.each(["failed", "degraded", "stopped"])("shows the banner when the state is %s", async (s) => {
    state.mockResolvedValue(s);
    renderShell();
    const banner = await screen.findByText(/backend is unavailable/i);
    expect(banner.closest('[role="alert"]')).not.toBeNull();
    expect(banner).toHaveTextContent(/changes are disabled/i);
    expect(banner).toHaveTextContent(/~\/\.portfolio_manager\/logs/);
    expect(screen.getByLabelText(`Sidecar ${s}`)).toBeInTheDocument();
  });

  it("tells the person why when the backend reports a reason, such as a full disk", async () => {
    state.mockResolvedValue("failed");
    failure.mockResolvedValue("Portfolio Manager could not open its database. The disk may be full.");
    renderShell();
    const banner = await screen.findByText(/disk may be full/i);
    expect(banner.closest('[role="alert"]')).toHaveTextContent(/backend is unavailable/i);
  });

  it("offers to restore the newest backup when the database is damaged", async () => {
    state.mockResolvedValue("failed");
    failure.mockResolvedValue("Portfolio Manager found that its database file is damaged and did not change it.");
    restoreLatest.mockResolvedValue(undefined);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderShell();
    await userEvent.click(await screen.findByRole("button", { name: /restore the latest backup/i }));
    expect(restoreLatest).toHaveBeenCalled();
    expect(await screen.findByText(/quit and reopen the app/i)).toBeInTheDocument();
  });

  it("does not offer a restore for other failures, such as a full disk", async () => {
    state.mockResolvedValue("failed");
    failure.mockResolvedValue("Portfolio Manager could not open its database. The disk may be full.");
    renderShell();
    await screen.findByText(/disk may be full/i);
    expect(screen.queryByRole("button", { name: /restore the latest backup/i })).not.toBeInTheDocument();
  });

  it("does not ask for a reason unless the backend failed", async () => {
    state.mockResolvedValue("degraded");
    renderShell();
    await screen.findByText(/backend is unavailable/i);
    expect(failure).not.toHaveBeenCalled();
  });

  it("disables write controls while degraded, and re-enables them on recovery", async () => {
    state.mockResolvedValue("ready");
    const { client } = renderShell();
    await userEvent.click(await screen.findByRole("tab", { name: /projects/i }));
    const newProject = await screen.findByRole("button", { name: /new project/i });
    expect(newProject).toBeEnabled();

    state.mockResolvedValue("degraded");
    await client.refetchQueries({ queryKey: ["sidecar", "state"] });
    await screen.findByText(/backend is unavailable/i);
    expect(screen.getByRole("button", { name: /new project/i })).toBeDisabled();
    // Navigation stays usable so the person can still look around.
    expect(screen.getByRole("tab", { name: /dashboard/i })).toBeEnabled();

    state.mockResolvedValue("ready");
    await client.refetchQueries({ queryKey: ["sidecar", "state"] });
    await waitFor(() => expect(screen.queryByText(/backend is unavailable/i)).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: /new project/i })).toBeEnabled();
  });

  it("explains a failed dashboard load in words, not [object Object]", async () => {
    state.mockResolvedValue("failed");
    dashboardGet.mockRejectedValue(SIDECAR_DOWN);
    renderShell();
    const alerts = await screen.findAllByRole("alert");
    const loadError = alerts.find((a) => /could not load dashboard/i.test(a.textContent ?? ""));
    expect(loadError).toBeDefined();
    expect(within(loadError as HTMLElement).getByText(/backend is not responding/i)).toBeInTheDocument();
    expect(loadError).not.toHaveTextContent("[object Object]");
  });

  it("shows a validation error from the backend to the user", async () => {
    state.mockResolvedValue("ready");
    dashboardGet.mockRejectedValue({ code: "INTERNAL_ERROR", message: "An unexpected error occurred.", correlation_id: "c-42" });
    renderShell();
    expect(await screen.findByText(/reference c-42/i)).toBeInTheDocument();
  });
});
