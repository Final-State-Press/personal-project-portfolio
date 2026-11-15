//! Sidecar process launch.
//!
//! Builds the command that starts the packaged FastAPI sidecar. The auth token
//! is passed via the `PORTFOLIO_SIDECAR_TOKEN` environment variable — never as a
//! command-line argument — so it does not appear in the process list. The port
//! is a non-secret argument.

use crate::errors::CommandError;
use std::io::Read;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};

/// Longest single log line forwarded to the app log; the rest is dropped.
const MAX_LOG_LINE: usize = 8 * 1024;

/// Description of how to start the sidecar process.
#[derive(Debug, Clone)]
pub struct SidecarCommand {
    /// Program to execute (packaged binary, or `python` in development).
    pub program: PathBuf,
    /// Arguments (excluding the token).
    pub args: Vec<String>,
}

impl SidecarCommand {
    /// Build the launch command for a packaged binary.
    ///
    /// # Arguments
    /// * `binary` - Path to the frozen `portfolio-sidecar` binary.
    /// * `port` - Loopback port to bind.
    /// * `production` - Whether to pass `--production`.
    pub fn packaged(binary: PathBuf, port: u16, production: bool) -> Self {
        let mut args = vec!["--port".into(), port.to_string()];
        if production {
            args.push("--production".into());
        }
        Self {
            program: binary,
            args,
        }
    }

    /// Assemble the argument vector actually passed to the OS (token excluded).
    ///
    /// Exposed for tests that assert the token is not present in argv.
    pub fn argv(&self) -> Vec<String> {
        let mut v = vec![self.program.to_string_lossy().to_string()];
        v.extend(self.args.clone());
        v
    }
}

/// Spawn the sidecar, passing the token via environment only.
///
/// # Errors
/// Returns `SIDECAR_STARTUP_FAILED` if the process cannot be spawned (e.g. the
/// binary is missing).
#[allow(clippy::result_large_err)]
pub fn spawn(cmd: &SidecarCommand, token: &str) -> Result<Child, CommandError> {
    let mut child = Command::new(&cmd.program)
        .args(&cmd.args)
        .env("PORTFOLIO_SIDECAR_TOKEN", token)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| {
            CommandError::sidecar_startup_failed(format!("failed to spawn sidecar: {e}"))
        })?;

    // The pipes must be drained for the life of the child. An OS pipe buffer is
    // about 64 KiB; once it fills, the sidecar blocks on its next log write and
    // the backend appears to hang.
    if let Some(out) = child.stdout.take() {
        drain_in_background(out, "stdout");
    }
    if let Some(err) = child.stderr.take() {
        drain_in_background(err, "stderr");
    }
    Ok(child)
}

/// Forward a child's output stream to the app log on a dedicated thread. The
/// thread ends when the child closes the stream (i.e. exits).
fn drain_in_background<R: Read + Send + 'static>(reader: R, stream: &'static str) {
    std::thread::Builder::new()
        .name(format!("sidecar-{stream}"))
        .spawn(move || {
            drain_lines(reader, |line| {
                tracing::info!(target: "sidecar", stream, "{line}");
            });
        })
        .ok();
}

/// Read `reader` to EOF, calling `sink` once per line.
///
/// Memory is bounded: lines longer than [`MAX_LOG_LINE`] are truncated, but the
/// stream is always read to the end so the writer never blocks.
pub(crate) fn drain_lines<R: Read>(mut reader: R, mut sink: impl FnMut(&str)) {
    let mut chunk = [0u8; 8192];
    let mut line: Vec<u8> = Vec::new();
    loop {
        let n = match reader.read(&mut chunk) {
            Ok(0) | Err(_) => break,
            Ok(n) => n,
        };
        for &b in &chunk[..n] {
            if b == b'\n' {
                sink(String::from_utf8_lossy(&line).trim_end_matches('\r'));
                line.clear();
            } else if line.len() < MAX_LOG_LINE {
                line.push(b);
            }
        }
    }
    if !line.is_empty() {
        sink(&String::from_utf8_lossy(&line));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drain_lines_splits_lines_and_bounds_long_ones() {
        let mut input = b"one\r\ntwo\n".to_vec();
        input.extend(std::iter::repeat_n(b'x', MAX_LOG_LINE * 3));
        input.extend(b"\nlast");
        let mut lines: Vec<String> = Vec::new();
        drain_lines(&input[..], |l| lines.push(l.to_string()));
        assert_eq!(lines[0], "one");
        assert_eq!(lines[1], "two");
        assert_eq!(lines[2].len(), MAX_LOG_LINE);
        assert_eq!(lines[3], "last");
    }

    /// Regression test: a chatty child must never block on a full pipe.
    /// Without draining, this child stalls at ~64 KiB per stream and never exits.
    #[test]
    fn chatty_child_is_not_blocked_by_full_pipes() {
        let cmd = SidecarCommand {
            program: PathBuf::from("/bin/sh"),
            args: vec![
                "-c".into(),
                "head -c 2000000 /dev/zero | tr '\\0' 'a' | fold -w 100; \
                 head -c 2000000 /dev/zero | tr '\\0' 'b' | fold -w 100 >&2"
                    .into(),
            ],
        };
        let mut child = spawn(&cmd, "t").expect("spawn");
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
        loop {
            if child.try_wait().expect("try_wait").is_some() {
                break;
            }
            if std::time::Instant::now() >= deadline {
                let _ = child.kill();
                panic!("child blocked on a full stdout/stderr pipe");
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
    }

    #[test]
    fn argv_excludes_token_and_includes_port() {
        let cmd = SidecarCommand::packaged(PathBuf::from("/opt/sidecar"), 8765, true);
        let argv = cmd.argv();
        assert!(argv.contains(&"8765".to_string()));
        assert!(argv.contains(&"--production".to_string()));
        assert!(
            !argv.iter().any(|a| a.contains("TOKEN") || a.len() == 64),
            "token must never appear in argv"
        );
    }
}
