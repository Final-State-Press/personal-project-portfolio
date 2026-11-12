//! Backup commands.

use crate::app_state::AppState;
use crate::errors::CommandError;
use crate::http::forwarder;
use crate::sidecar::supervisor::SidecarState;
use reqwest::Method;
use serde_json::Value;
use tauri::State;

/// `GET /api/v1/backups` — available backups and the folder that holds them.
#[tauri::command]
pub async fn backups_list(state: State<'_, AppState>) -> Result<Value, CommandError> {
    forwarder::get(&state, "/api/v1/backups").await
}

/// `POST /api/v1/backups` — make a backup now.
#[tauri::command]
pub async fn backup_create(state: State<'_, AppState>) -> Result<Value, CommandError> {
    forwarder::forward(&state, Method::POST, "/api/v1/backups", None::<&Value>).await
}

/// `POST /api/v1/backups/restore` — restore a backup the next time the app starts.
#[tauri::command]
pub async fn backup_restore(
    state: State<'_, AppState>,
    payload: Value,
) -> Result<Value, CommandError> {
    forwarder::forward(
        &state,
        Method::POST,
        "/api/v1/backups/restore",
        Some(&payload),
    )
    .await
}

/// `POST /api/v1/backups/export` — write a copy of the database into a chosen folder.
#[tauri::command]
pub async fn backup_export(
    state: State<'_, AppState>,
    payload: Value,
) -> Result<Value, CommandError> {
    forwarder::forward(
        &state,
        Method::POST,
        "/api/v1/backups/export",
        Some(&payload),
    )
    .await
}

/// Replace the database with the newest backup. Only allowed while the backend is
/// down (it holds the database open otherwise); the app must be restarted after.
///
/// This is the way out when the database is too damaged for the backend to start.
#[tauri::command]
pub async fn backup_restore_latest(state: State<'_, AppState>) -> Result<(), CommandError> {
    if state.current_state() != SidecarState::Failed {
        return Err(CommandError::new(
            "CONFLICT",
            "The backend is running. Use the Backups list in Settings instead.",
        ));
    }
    let binary = crate::resolve_sidecar_binary().ok_or_else(|| {
        CommandError::new("INTERNAL_ERROR", "The backend program could not be found.")
    })?;
    let status = tauri::async_runtime::spawn_blocking(move || {
        std::process::Command::new(binary)
            .arg("--restore-latest")
            .stdin(std::process::Stdio::null())
            .status()
    })
    .await
    .map_err(|e| CommandError::new("INTERNAL_ERROR", format!("restore did not run: {e}")))?
    .map_err(|e| CommandError::new("INTERNAL_ERROR", format!("restore did not run: {e}")))?;
    if status.success() {
        Ok(())
    } else {
        Err(CommandError::new(
            "DATABASE_ERROR",
            "There is no backup to restore, or it could not be restored. Your data was not changed.",
        ))
    }
}
