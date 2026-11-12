import { invoke } from "./invoke";

export interface BackupItem {
  name: string;
  created_at: string;
  size_bytes: number;
}

export interface BackupList {
  folder: string;
  backups: BackupItem[];
  pending_restore?: string | null;
}

/** Available backups, newest first, and the folder that holds them. */
export function list(): Promise<BackupList> {
  return invoke("backups_list");
}

/** Make a backup now. */
export function create(): Promise<BackupItem> {
  return invoke("backup_create");
}

/** Restore a backup the next time the app starts. */
export function restore(name: string): Promise<{ name: string; restart_required: boolean }> {
  return invoke("backup_restore", { payload: { name } });
}

/** Write a verified copy of the database into an existing folder. */
export function exportTo(destination: string): Promise<{ path: string }> {
  return invoke("backup_export", { payload: { destination } });
}

/** Replace the database with the newest backup. Only works while the backend is down. */
export function restoreLatest(): Promise<void> {
  return invoke("backup_restore_latest");
}
