import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { open } from "@tauri-apps/plugin-dialog";
import { backups as api, describeError } from "@/command-client";

function formatSize(bytes: number): string {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** Back up now, see the backups that exist, and where they are. */
export function BackupsPanel() {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["backups"], queryFn: () => api.list() });
  const create = useMutation({
    mutationFn: () => api.create(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["backups"] }),
  });

  const restore = useMutation({
    mutationFn: (name: string) => api.restore(name),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["backups"] }),
  });
  const exportCopy = useMutation({
    mutationFn: async () => {
      const folder = await open({ directory: true, multiple: false, title: "Export a copy of your data to…" });
      if (typeof folder !== "string") return null; // cancelled
      return api.exportTo(folder);
    },
  });

  const ask = (name: string, when: string) => {
    const ok = window.confirm(
      `Restore the backup from ${when}? Anything you entered since then will be replaced when you ` +
        "restart the app. Your current data is kept aside.",
    );
    if (ok) restore.mutate(name);
  };

  return (
    <section aria-label="Backups" style={{ marginTop: "var(--space-5)", maxWidth: 480 }}>
      <h3>Backups</h3>
      <p>
        A backup is made automatically once a day, and the newest 7 are kept. You can also make one
        now.
      </p>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <button
          type="button"
          className="btn"
          disabled={create.isPending}
          onClick={() => create.mutate()}
        >
          Back up now
        </button>
        <button
          type="button"
          className="btn"
          disabled={exportCopy.isPending}
          onClick={() => exportCopy.mutate()}
        >
          Export a copy…
        </button>
        {create.isSuccess && <span role="status">Backup saved.</span>}
        {exportCopy.data && <span role="status">Exported to {exportCopy.data.path}</span>}
      </div>
      {exportCopy.isError && (
        <p className="field-error" role="alert">
          {describeError(exportCopy.error)} — nothing was exported.
        </p>
      )}
      {restore.isError && (
        <p className="field-error" role="alert">
          {describeError(restore.error)} — nothing was restored.
        </p>
      )}
      {query.data?.pending_restore && (
        <p role="status" className="banner">
          A restore is waiting. <strong>Quit and reopen the app</strong> to apply it.
        </p>
      )}
      {create.isError && (
        <p className="field-error" role="alert">
          {describeError(create.error)} — no backup was made.
        </p>
      )}
      {query.isError && (
        <p className="field-error" role="alert">
          Could not list backups. {describeError(query.error)}
        </p>
      )}
      {query.data && (
        <>
          <small className="field-hint">
            Folder: <code>{query.data.folder}</code>
          </small>
          {query.data.backups.length === 0 ? (
            <p>No backups yet.</p>
          ) : (
            <ul aria-label="Backup files">
              {query.data.backups.map((b) => (
                <li key={b.name}>
                  {new Date(b.created_at).toLocaleString()} · {formatSize(b.size_bytes)}{" "}
                  <button
                    type="button"
                    className="btn"
                    disabled={restore.isPending}
                    onClick={() => ask(b.name, new Date(b.created_at).toLocaleString())}
                  >
                    Restore
                  </button>
                </li>
              ))}
            </ul>
          )}
          <small className="field-hint">
            Restoring takes effect when you next open the app. The data it replaces is kept in the
            same folder as <code>before-restore-…</code>.
          </small>
        </>
      )}
    </section>
  );
}
