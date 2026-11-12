import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const list = vi.fn();
const create = vi.fn();
const restore = vi.fn();
const exportTo = vi.fn();
const openDialog = vi.fn();

vi.mock("@tauri-apps/plugin-dialog", () => ({ open: (...a: unknown[]) => openDialog(...a) }));

vi.mock("@/command-client", async () => {
  const actual = await vi.importActual<typeof import("@/command-client")>("@/command-client");
  return { ...actual, backups: { list: () => list(), create: () => create(), restore: (n: string) => restore(n), exportTo: (d: string) => exportTo(d) } };
});

import { BackupsPanel } from "./BackupsPanel";

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <BackupsPanel />
    </QueryClientProvider>,
  );
}

const ITEM = { name: "portfolio-1.db", created_at: "2026-11-10T12:00:00+00:00", size_bytes: 2048 };

describe("BackupsPanel", () => {
  beforeEach(() => {
    list.mockReset();
    create.mockReset();
    restore.mockReset();
    exportTo.mockReset();
    openDialog.mockReset();
  });

  it("shows the folder and the backups that exist", async () => {
    list.mockResolvedValue({ folder: "/home/me/.portfolio_manager/backups", backups: [ITEM] });
    renderPanel();
    expect(await screen.findByText("/home/me/.portfolio_manager/backups")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: /backup files/i })).toHaveTextContent("2 KB");
  });

  it("says so when there are no backups", async () => {
    list.mockResolvedValue({ folder: "/x", backups: [] });
    renderPanel();
    expect(await screen.findByText(/no backups yet/i)).toBeInTheDocument();
  });

  it("makes a backup and refreshes the list", async () => {
    list.mockResolvedValueOnce({ folder: "/x", backups: [] });
    list.mockResolvedValue({ folder: "/x", backups: [ITEM] });
    create.mockResolvedValue(ITEM);
    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: /back up now/i }));
    expect(await screen.findByRole("status")).toHaveTextContent(/backup saved/i);
    await waitFor(() => expect(screen.getByRole("list", { name: /backup files/i })).toBeInTheDocument());
  });

  it("tells the person plainly when the backup failed", async () => {
    list.mockResolvedValue({ folder: "/x", backups: [] });
    create.mockRejectedValue({ code: "DISK_FULL", message: "Your disk is full, so this change was not saved." });
    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: /back up now/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/disk is full/i);
    expect(alert).toHaveTextContent(/no backup was made/i);
  });

  it("restores a backup after confirmation and says a restart is needed", async () => {
    list.mockResolvedValueOnce({ folder: "/x", backups: [ITEM] });
    list.mockResolvedValue({ folder: "/x", backups: [ITEM], pending_restore: ITEM.name });
    restore.mockResolvedValue({ name: ITEM.name, restart_required: true });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: /^restore$/i }));
    expect(restore).toHaveBeenCalledWith(ITEM.name);
    expect(await screen.findByText(/quit and reopen the app/i)).toBeInTheDocument();
  });

  it("does nothing when the person declines to restore", async () => {
    list.mockResolvedValue({ folder: "/x", backups: [ITEM] });
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: /^restore$/i }));
    expect(restore).not.toHaveBeenCalled();
  });

  it("exports to the folder the person picks, and does nothing if they cancel", async () => {
    list.mockResolvedValue({ folder: "/x", backups: [] });
    exportTo.mockResolvedValue({ path: "/Volumes/USB/portfolio-export-1.db" });
    openDialog.mockResolvedValueOnce(null);
    renderPanel();
    const btn = await screen.findByRole("button", { name: /export a copy/i });
    await userEvent.click(btn);
    await waitFor(() => expect(openDialog).toHaveBeenCalledTimes(1));
    expect(exportTo).not.toHaveBeenCalled();

    openDialog.mockResolvedValueOnce("/Volumes/USB");
    await userEvent.click(btn);
    expect(await screen.findByText(/exported to \/Volumes\/USB\/portfolio-export-1\.db/i)).toBeInTheDocument();
    expect(exportTo).toHaveBeenCalledWith("/Volumes/USB");
  });
});
