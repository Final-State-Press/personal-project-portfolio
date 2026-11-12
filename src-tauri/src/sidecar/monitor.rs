//! Post-startup sidecar monitoring.
//!
//! Runs for the life of the app after the sidecar is launched and keeps the
//! supervisor state honest, so the UI's degraded banner reflects reality:
//!
//! * **Exit detection.** If the process exits on its own (crash, kill, OOM) the
//!   state moves to `Failed` immediately and the exit status is logged. A
//!   deliberate shutdown is recognized and not reported as a failure.
//! * **Health checking.** While the process is alive, the identity-checked
//!   `/health` probe runs periodically. After [`FAILURES_BEFORE_DEGRADED`]
//!   consecutive failures (a hung or impostor listener) the state becomes
//!   `Degraded`; the first success returns it to `Ready`.

use crate::sidecar::readiness::{probe_once, ProbeResult};
use crate::sidecar::supervisor::{ChildStatus, SidecarHandle, SidecarState};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// Consecutive failed probes before the state becomes `Degraded`.
pub const FAILURES_BEFORE_DEGRADED: u32 = 3;

/// Monitoring cadence.
#[derive(Debug, Clone, Copy)]
pub struct MonitorConfig {
    /// How often to check whether the process is still alive.
    pub exit_poll: Duration,
    /// Run a health probe once every this many exit polls.
    pub probe_every: u32,
}

impl Default for MonitorConfig {
    fn default() -> Self {
        Self {
            exit_poll: Duration::from_secs(1),
            probe_every: 5,
        }
    }
}

/// Shared slot holding a plain-language reason for the last failure, if known.
pub type FailureReason = Arc<Mutex<Option<String>>>;

/// Turn a sidecar exit code into a message for the user.
///
/// The codes are defined by the sidecar (`cli/sidecar.py`). Unknown codes return
/// `None`, and the UI falls back to its generic "backend is not responding" text.
pub fn exit_reason(code: Option<i32>) -> Option<String> {
    match code? {
        74 => Some(
            "Portfolio Manager could not open its database. The disk may be full, or the \
             database folder is not writable. Free up space, then restart the app."
                .into(),
        ),
        68 => Some(
            "Portfolio Manager found that its database file is damaged and did not change it. \
             If you have a backup, restore it. Upgrade backups are the .bak files next to \
             the database."
                .into(),
        ),
        67 => Some(
            "This data was created by a newer version of Portfolio Manager. Install the \
             latest version. Your data has not been changed."
                .into(),
        ),
        65 => Some(
            "Portfolio Manager could not read or upgrade its database. Your data was not \
             changed. Restart the app; if this repeats, see the log and the backup files \
             next to the database."
                .into(),
        ),
        _ => None,
    }
}

fn set(state: &Arc<Mutex<SidecarState>>, new: SidecarState) {
    if let Ok(mut guard) = state.lock() {
        *guard = new;
    }
}

fn get(state: &Arc<Mutex<SidecarState>>) -> SidecarState {
    state.lock().map(|g| *g).unwrap_or(SidecarState::Failed)
}

/// Monitor the sidecar until it exits or the app shuts it down.
pub async fn run(
    handle: Arc<SidecarHandle>,
    state: Arc<Mutex<SidecarState>>,
    client: reqwest::Client,
    base_url: String,
    token: String,
    failure: FailureReason,
    cfg: MonitorConfig,
) {
    let mut ticks: u32 = 0;
    let mut failures: u32 = 0;
    loop {
        tokio::time::sleep(cfg.exit_poll).await;

        match handle.poll() {
            ChildStatus::ShutDown => return,
            ChildStatus::Exited(code) => {
                tracing::error!(
                    exit_code = ?code,
                    "sidecar process exited unexpectedly; the backend is unavailable"
                );
                if let Ok(mut slot) = failure.lock() {
                    *slot = exit_reason(code);
                }
                set(&state, SidecarState::Failed);
                return;
            }
            ChildStatus::Running => {}
        }

        ticks = ticks.wrapping_add(1);
        if ticks % cfg.probe_every.max(1) != 0 {
            continue;
        }
        // Only judge health once startup has finished; readiness polling owns
        // the Starting state.
        let current = get(&state);
        if current != SidecarState::Ready && current != SidecarState::Degraded {
            continue;
        }

        match probe_once(&client, &base_url, &token).await {
            ProbeResult::Proven => {
                if failures >= FAILURES_BEFORE_DEGRADED {
                    tracing::info!("sidecar health restored");
                }
                failures = 0;
                set(&state, SidecarState::Ready);
            }
            _ => {
                failures += 1;
                if failures == FAILURES_BEFORE_DEGRADED {
                    tracing::warn!(
                        "sidecar failed {failures} health checks in a row; marking degraded"
                    );
                }
                if failures >= FAILURES_BEFORE_DEGRADED {
                    set(&state, SidecarState::Degraded);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    fn fast() -> MonitorConfig {
        MonitorConfig {
            exit_poll: Duration::from_millis(20),
            probe_every: 1,
        }
    }

    fn client() -> reqwest::Client {
        reqwest::Client::builder()
            .connect_timeout(Duration::from_millis(100))
            .timeout(Duration::from_millis(300))
            .build()
            .unwrap()
    }

    fn handle_for(script: &str) -> Arc<SidecarHandle> {
        let child = Command::new("/bin/sh")
            .args(["-c", script])
            .spawn()
            .unwrap();
        Arc::new(SidecarHandle::new(child))
    }

    async fn wait_state(state: &Arc<Mutex<SidecarState>>, want: SidecarState) -> bool {
        for _ in 0..100 {
            if get(state) == want {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        false
    }

    #[test]
    fn exit_codes_map_to_messages() {
        assert!(exit_reason(Some(74)).unwrap().contains("disk may be full"));
        assert!(exit_reason(Some(65)).unwrap().contains("not changed"));
        assert!(exit_reason(Some(68)).unwrap().contains("damaged"));
        assert!(exit_reason(Some(67)).unwrap().contains("newer version"));
        assert_eq!(exit_reason(Some(1)), None);
        assert_eq!(exit_reason(None), None);
    }

    #[tokio::test]
    async fn exit_code_becomes_the_failure_reason() {
        let handle = handle_for("exit 74");
        let state = Arc::new(Mutex::new(SidecarState::Starting));
        let failure: FailureReason = Arc::default();
        let task = tokio::spawn(run(
            handle,
            state.clone(),
            client(),
            "http://127.0.0.1:1".into(),
            "t".into(),
            failure.clone(),
            fast(),
        ));
        assert!(wait_state(&state, SidecarState::Failed).await);
        task.await.unwrap();
        assert_eq!(*failure.lock().unwrap(), exit_reason(Some(74)));
    }

    #[tokio::test]
    async fn crash_moves_state_to_failed() {
        let handle = handle_for("exit 1");
        let state = Arc::new(Mutex::new(SidecarState::Ready));
        let task = tokio::spawn(run(
            handle,
            state.clone(),
            client(),
            "http://127.0.0.1:1".into(),
            "t".into(),
            Arc::default(),
            fast(),
        ));
        assert!(wait_state(&state, SidecarState::Failed).await);
        task.await.unwrap();
    }

    #[tokio::test]
    async fn deliberate_shutdown_is_not_a_failure() {
        let handle = handle_for("sleep 30");
        let state = Arc::new(Mutex::new(SidecarState::Ready));
        let task = tokio::spawn(run(
            handle.clone(),
            state.clone(),
            client(),
            "http://127.0.0.1:1".into(),
            "t".into(),
            Arc::default(),
            // Probing every tick would degrade the state; shutdown must win first.
            MonitorConfig {
                exit_poll: Duration::from_millis(20),
                probe_every: 1000,
            },
        ));
        handle.shutdown();
        task.await.unwrap();
        assert_eq!(get(&state), SidecarState::Ready);
    }

    #[tokio::test]
    async fn unresponsive_but_alive_sidecar_becomes_degraded() {
        let handle = handle_for("sleep 30");
        let state = Arc::new(Mutex::new(SidecarState::Ready));
        let task = tokio::spawn(run(
            handle.clone(),
            state.clone(),
            client(),
            "http://127.0.0.1:1".into(),
            "t".into(),
            Arc::default(),
            fast(),
        ));
        assert!(wait_state(&state, SidecarState::Degraded).await);
        handle.shutdown();
        task.await.unwrap();
    }

    #[tokio::test]
    async fn starting_state_is_left_to_readiness_polling() {
        let handle = handle_for("sleep 30");
        let state = Arc::new(Mutex::new(SidecarState::Starting));
        let task = tokio::spawn(run(
            handle.clone(),
            state.clone(),
            client(),
            "http://127.0.0.1:1".into(),
            "t".into(),
            Arc::default(),
            fast(),
        ));
        tokio::time::sleep(Duration::from_millis(300)).await;
        assert_eq!(get(&state), SidecarState::Starting);
        handle.shutdown();
        task.await.unwrap();
    }
}
