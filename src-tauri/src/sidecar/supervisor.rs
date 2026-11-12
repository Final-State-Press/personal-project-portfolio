//! Sidecar supervisor.
//!
//! Encapsulates the sidecar lifecycle: port selection, token generation, launch,
//! readiness polling, monitoring, and shutdown. Exposes an explicit state
//! machine so the UI can distinguish "starting", "ready", and "failed".

use crate::security::{port, token};
use serde::Serialize;
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

/// Supervisor lifecycle states.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SidecarState {
    /// Not yet started.
    NotStarted,
    /// Launch initiated; awaiting readiness.
    Starting,
    /// Healthy and serving requests.
    Ready,
    /// Running but unhealthy (e.g. lost readiness).
    Degraded,
    /// Shutdown initiated.
    Stopping,
    /// Cleanly stopped.
    Stopped,
    /// Startup or runtime failure.
    Failed,
}

/// Prepared launch parameters (port + token) before spawning.
pub struct LaunchPlan {
    /// Selected loopback port.
    pub port: u16,
    /// Generated per-launch token.
    pub token: String,
}

impl LaunchPlan {
    /// Allocate a free loopback port and generate a fresh token.
    ///
    /// # Errors
    /// Returns an error string if no free port can be allocated.
    pub fn prepare() -> Result<Self, String> {
        let port = port::pick_free_port().map_err(|e| format!("port allocation failed: {e}"))?;
        Ok(Self {
            port,
            token: token::generate_token(),
        })
    }
}

/// What a non-blocking look at the child process found.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChildStatus {
    /// Still running.
    Running,
    /// Exited on its own. Carries the exit code, or `None` if it was killed by a signal.
    Exited(Option<i32>),
    /// The app asked it to stop; not a failure.
    ShutDown,
}

/// Owns the spawned child process and tracks shutdown.
pub struct SidecarHandle {
    child: Arc<Mutex<Option<Child>>>,
    shutting_down: AtomicBool,
}

impl SidecarHandle {
    /// Wrap a spawned child process.
    pub fn new(child: Child) -> Self {
        Self {
            child: Arc::new(Mutex::new(Some(child))),
            shutting_down: AtomicBool::new(false),
        }
    }

    /// Terminate the child if still running.
    pub fn shutdown(&self) {
        self.shutting_down.store(true, Ordering::SeqCst);
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = crate::sidecar::shutdown::terminate(&mut child);
                let _ = child.wait();
            }
        }
    }

    /// Non-blocking status of the child, distinguishing a crash from a deliberate shutdown.
    pub fn poll(&self) -> ChildStatus {
        if self.shutting_down.load(Ordering::SeqCst) {
            return ChildStatus::ShutDown;
        }
        let Ok(mut guard) = self.child.lock() else {
            return ChildStatus::Exited(None);
        };
        match guard.as_mut() {
            None => ChildStatus::ShutDown,
            Some(child) => match child.try_wait() {
                Ok(Some(status)) => ChildStatus::Exited(status.code()),
                Ok(None) => ChildStatus::Running,
                Err(_) => ChildStatus::Exited(None),
            },
        }
    }

    /// Return `Some(success)` if the child has exited, else `None`.
    pub fn check_exited(&self) -> Option<bool> {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(child) = guard.as_mut() {
                return crate::sidecar::shutdown::exited(child);
            }
        }
        Some(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn launch_plan_has_port_and_token() {
        let plan = LaunchPlan::prepare().expect("prepare");
        assert!(plan.port > 0);
        assert_eq!(plan.token.len(), 64);
    }

    fn spawn_sh(script: &str) -> SidecarHandle {
        let child = std::process::Command::new("/bin/sh")
            .args(["-c", script])
            .spawn()
            .expect("spawn sh");
        SidecarHandle::new(child)
    }

    fn wait_for(handle: &SidecarHandle, want: ChildStatus) -> ChildStatus {
        for _ in 0..100 {
            let got = handle.poll();
            if got == want {
                return got;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        handle.poll()
    }

    #[test]
    fn poll_reports_running_then_exit_code() {
        let h = spawn_sh("sleep 0.2; exit 3");
        assert_eq!(h.poll(), ChildStatus::Running);
        assert_eq!(
            wait_for(&h, ChildStatus::Exited(Some(3))),
            ChildStatus::Exited(Some(3))
        );
    }

    #[test]
    fn poll_reports_signal_death_as_exit_without_code() {
        let h = spawn_sh("kill -9 $$");
        assert_eq!(
            wait_for(&h, ChildStatus::Exited(None)),
            ChildStatus::Exited(None)
        );
    }

    #[test]
    fn deliberate_shutdown_is_not_reported_as_a_crash() {
        let h = spawn_sh("sleep 30");
        h.shutdown();
        assert_eq!(h.poll(), ChildStatus::ShutDown);
    }

    #[test]
    fn state_serializes_to_snake_case() {
        let json = serde_json::to_string(&SidecarState::NotStarted).unwrap();
        assert_eq!(json, "\"not_started\"");
    }
}
