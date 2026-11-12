//! Sidecar termination.
//!
//! Ensures the child process is killed when the application exits so no orphaned
//! sidecar keeps the database open.

use std::process::Child;
use std::time::{Duration, Instant};

/// How long the sidecar gets to close its database cleanly before it is killed.
pub const GRACE_PERIOD: Duration = Duration::from_secs(2);

/// Terminate the sidecar child process, ignoring "already exited" errors.
///
/// Sends `SIGTERM` first so the sidecar can finish in-flight requests, checkpoint
/// the write-ahead log and close the database. If it has not exited within
/// [`GRACE_PERIOD`], it is killed.
///
/// # Returns
/// `true` if the process exited (cleanly or by being killed), or had already exited.
pub fn terminate(child: &mut Child) -> bool {
    terminate_with_grace(child, GRACE_PERIOD)
}

/// Like [`terminate`], with an explicit grace period (used by tests).
pub fn terminate_with_grace(child: &mut Child, grace: Duration) -> bool {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return true;
    }
    // SAFETY: `kill` with a valid pid and signal number has no memory-safety
    // requirements; the pid belongs to a child we still own (not yet reaped).
    let sent = unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) } == 0;
    if sent {
        let deadline = Instant::now() + grace;
        while Instant::now() < deadline {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    }
    child.kill().is_ok()
}

/// Detect whether the child has exited unexpectedly.
///
/// # Returns
/// `Some(success)` if the process has exited, `None` if still running.
pub fn exited(child: &mut Child) -> Option<bool> {
    match child.try_wait() {
        Ok(Some(status)) => Some(status.success()),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    #[test]
    fn terminate_kills_a_long_running_child() {
        // `sleep 30` is a cheap long-running child on Unix.
        let mut child = Command::new("sleep")
            .arg("30")
            .spawn()
            .expect("spawn sleep");
        assert!(exited(&mut child).is_none());
        assert!(terminate(&mut child));
        let _ = child.wait();
    }

    #[test]
    fn terminate_is_graceful_for_a_child_that_handles_sigterm() {
        // The shell traps SIGTERM, exits 7 after a moment: proves SIGTERM came first.
        let mut child = Command::new("/bin/sh")
            .args(["-c", "trap 'exit 7' TERM; while :; do sleep 0.05; done"])
            .spawn()
            .unwrap();
        std::thread::sleep(Duration::from_millis(200));
        assert!(terminate_with_grace(&mut child, Duration::from_secs(3)));
        assert_eq!(child.wait().unwrap().code(), Some(7));
    }

    #[test]
    fn terminate_kills_a_child_that_ignores_sigterm_after_the_grace_period() {
        let mut child = Command::new("/bin/sh")
            .args(["-c", "trap '' TERM; while :; do sleep 0.05; done"])
            .spawn()
            .unwrap();
        std::thread::sleep(Duration::from_millis(200));
        let started = Instant::now();
        assert!(terminate_with_grace(&mut child, Duration::from_millis(300)));
        let status = child.wait().unwrap();
        assert!(status.code().is_none(), "should die by signal (SIGKILL)");
        assert!(started.elapsed() < Duration::from_secs(3));
    }

    #[test]
    fn exited_reports_completed_child() {
        let mut child = Command::new("true").spawn().expect("spawn true");
        let _ = child.wait();
        assert_eq!(exited(&mut child), Some(true));
    }
}
