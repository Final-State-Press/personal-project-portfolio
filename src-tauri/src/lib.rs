//! Portfolio Manager Tauri shell library.
//!
//! Boots the sidecar supervisor, wires runtime state, and registers the explicit
//! command allowlist. The renderer reaches the backend only through these
//! commands; the sidecar port and token never leave Rust in-memory state.

pub mod app_state;
pub mod commands;
pub mod errors;
pub mod http;
pub mod logging;
pub mod security;
pub mod sidecar;

use std::env;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use app_state::AppState;
use sidecar::launcher::{spawn, SidecarCommand};
use sidecar::monitor::{self, MonitorConfig};
use sidecar::readiness::{wait_until_ready, ReadinessOutcome};
use sidecar::supervisor::{LaunchPlan, SidecarHandle, SidecarState};
use tauri::Manager;

/// Whether this build enables production hardening (release profile).
fn is_production() -> bool {
    !cfg!(debug_assertions)
}

/// Candidate sidecar locations, relative to the directory holding the running
/// executable, in preference order.
///
/// The one-directory PyInstaller build starts in well under a second and is
/// bundled as a resource, so it is tried first. The single-file build re-extracts
/// itself on every launch — roughly ten seconds — and is kept only as a fallback
/// for bundles produced by `build_sidecar.py --mode onefile`.
const SIDECAR_CANDIDATES: [&str; 4] = [
    // macOS bundle: Contents/MacOS/ -> Contents/Resources/
    "../Resources/binaries/portfolio-sidecar/portfolio-sidecar",
    // Running straight out of a build directory.
    "binaries/portfolio-sidecar/portfolio-sidecar",
    // onefile, placed beside the executable by Tauri's externalBin.
    "portfolio-sidecar",
    "binaries/portfolio-sidecar",
];

/// Resolve the packaged sidecar binary.
///
/// Resolution order:
/// 1. `PORTFOLIO_SIDECAR_BINARY` environment override (used in development).
/// 2. Each entry of [`SIDECAR_CANDIDATES`] beside the current executable.
pub(crate) fn resolve_sidecar_binary() -> Option<PathBuf> {
    if let Ok(p) = env::var("PORTFOLIO_SIDECAR_BINARY") {
        let path = PathBuf::from(p);
        if path.exists() {
            return Some(path);
        }
    }
    let exe = env::current_exe().ok()?;
    let dir = exe.parent()?;
    SIDECAR_CANDIDATES
        .iter()
        .map(|name| dir.join(name))
        .find(|candidate| candidate.is_file())
}

/// Bring the running app's window to the front (used when a second launch is attempted).
fn focus_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.webview_windows().values().next() {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Application entry point invoked from `main`.
pub fn run() {
    logging::init();

    tauri::Builder::default()
        // Must be registered first. A second launch would start a second sidecar on
        // the same database (slow, failing saves; two writers on the config file),
        // so it hands over to the running app and exits instead.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            focus_main_window(app);
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let http = http::client::build_client();

            let plan = match LaunchPlan::prepare() {
                Ok(p) => p,
                Err(e) => {
                    tracing::error!("failed to prepare launch: {e}");
                    let state = AppState::new(0, String::new(), http.clone());
                    state.set_state(SidecarState::Failed);
                    app.manage(state);
                    return Ok(());
                }
            };

            let app_state = AppState::new(plan.port, plan.token.clone(), http.clone());

            match resolve_sidecar_binary() {
                Some(binary) => {
                    let cmd = SidecarCommand::packaged(binary, plan.port, is_production());
                    match spawn(&cmd, &plan.token) {
                        Ok(child) => {
                            let handle = Arc::new(SidecarHandle::new(child));
                            app.manage(handle.clone());

                            // Poll readiness in the background and update state.
                            let state_arc = app_state.state.clone();
                            let base_url = app_state.base_url();
                            let poll_http = http.clone();
                            let poll_token = plan.token.clone();

                            // Watch for the sidecar dying or hanging for the life of the app.
                            tauri::async_runtime::spawn(monitor::run(
                                handle.clone(),
                                app_state.state.clone(),
                                http.clone(),
                                app_state.base_url(),
                                plan.token.clone(),
                                app_state.failure.clone(),
                                MonitorConfig::default(),
                            ));

                            tauri::async_runtime::spawn(async move {
                                let outcome = wait_until_ready(
                                    &poll_http,
                                    &base_url,
                                    &poll_token,
                                    Duration::from_secs(30),
                                    Duration::from_millis(250),
                                )
                                .await;
                                let new = match outcome {
                                    ReadinessOutcome::Ready => SidecarState::Ready,
                                    ReadinessOutcome::TimedOut => SidecarState::Failed,
                                    ReadinessOutcome::IdentityMismatch => {
                                        tracing::error!(
                                            "a process on the sidecar port failed the identity check; \
                                             refusing to use it"
                                        );
                                        SidecarState::Failed
                                    }
                                };
                                if let Ok(mut guard) = state_arc.lock() {
                                    *guard = new;
                                }
                            });
                        }
                        Err(e) => {
                            tracing::error!("sidecar spawn failed: {e}");
                            app_state.set_state(SidecarState::Failed);
                        }
                    }
                }
                None => {
                    tracing::error!("sidecar binary not found");
                    app_state.set_state(SidecarState::Failed);
                }
            }

            app.manage(app_state);
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(handle) = window.app_handle().try_state::<Arc<SidecarHandle>>() {
                    handle.shutdown();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::health::sidecar_health,
            commands::health::sidecar_state,
            commands::health::sidecar_failure,
            commands::dashboard::dashboard_get,
            commands::projects::projects_list,
            commands::projects::project_get,
            commands::projects::project_create,
            commands::projects::project_update,
            commands::projects::project_archive,
            commands::projects::project_delete,
            commands::projects::project_plan_get,
            commands::projects::project_plan_save,
            commands::sessions::sessions_for_week,
            commands::sessions::session_create,
            commands::sessions::session_update,
            commands::sessions::session_set_status,
            commands::sessions::session_reschedule,
            commands::sessions::session_delete,
            commands::milestones::milestones_for_project,
            commands::milestones::milestone_create,
            commands::milestones::milestone_update,
            commands::milestones::milestone_set_status,
            commands::milestones::milestone_delete,
            commands::reviews::reviews_list,
            commands::reviews::review_get_or_create,
            commands::reviews::review_save,
            commands::scores::score_override,
            commands::backups::backups_list,
            commands::backups::backup_create,
            commands::backups::backup_restore,
            commands::backups::backup_export,
            commands::backups::backup_restore_latest,
            commands::settings::settings_get,
            commands::settings::settings_update,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Portfolio Manager")
        .run(|app, event| {
            // The window-destroyed hook does not cover every way of quitting (Cmd-Q
            // with no window, a programmatic exit), so stop the sidecar here too.
            if let tauri::RunEvent::Exit = event {
                if let Some(handle) = app.try_state::<Arc<SidecarHandle>>() {
                    handle.shutdown();
                }
            }
        });
}
