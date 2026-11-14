//! Readiness polling with identity verification.
//!
//! Repeatedly probes the sidecar's unauthenticated `/health` endpoint until it
//! responds or a timeout elapses. `/health` is used (not `/ready`) so liveness
//! is confirmed even before the database is fully ready.
//!
//! Each probe carries a random nonce. The sidecar must answer with
//! `HMAC-SHA256(token, nonce)` (see [`crate::security::identity`]). A process
//! that merely answers 200 on the port, such as one that won the port race, is
//! never treated as ready, so the auth token is never sent to it.

use crate::security::identity::{generate_nonce, verify_proof, CHALLENGE_HEADER};
use std::time::{Duration, Instant};

/// Outcome of waiting for the sidecar to become ready.
#[derive(Debug, PartialEq, Eq)]
pub enum ReadinessOutcome {
    /// The sidecar responded healthy and proved it holds the token.
    Ready,
    /// The deadline elapsed with no response at all.
    TimedOut,
    /// Something answered on the port but could not prove it is our sidecar.
    IdentityMismatch,
}

/// Result of one identity-checked probe of `/health`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeResult {
    /// Healthy and proved it holds the token.
    Proven,
    /// Answered, but with a bad status or no valid proof.
    Unproven,
    /// No answer (connection refused, timeout, ...).
    NoResponse,
}

/// Probe `{base_url}/health` once, with a fresh nonce, and verify the proof.
pub async fn probe_once(client: &reqwest::Client, base_url: &str, token: &str) -> ProbeResult {
    let nonce = generate_nonce();
    let Ok(resp) = client
        .get(format!("{base_url}/health"))
        .header(CHALLENGE_HEADER, &nonce)
        .send()
        .await
    else {
        return ProbeResult::NoResponse;
    };
    if !resp.status().is_success() {
        return ProbeResult::Unproven;
    }
    let proof = resp
        .json::<serde_json::Value>()
        .await
        .ok()
        .and_then(|v| v.get("proof").and_then(|p| p.as_str().map(str::to_owned)));
    match proof {
        Some(p) if verify_proof(token, &nonce, &p) => ProbeResult::Proven,
        _ => ProbeResult::Unproven,
    }
}

/// Poll `{base_url}/health` until the sidecar proves its identity or `timeout`
/// elapses.
///
/// # Arguments
/// * `client` - Shared HTTP client.
/// * `base_url` - Sidecar base URL.
/// * `token` - The per-launch token the real sidecar was started with. Only used
///   to verify the proof locally; it is never sent.
/// * `timeout` - Overall deadline.
/// * `interval` - Delay between polls.
pub async fn wait_until_ready(
    client: &reqwest::Client,
    base_url: &str,
    token: &str,
    timeout: Duration,
    interval: Duration,
) -> ReadinessOutcome {
    let deadline = Instant::now() + timeout;
    let mut saw_unproven_response = false;
    loop {
        match probe_once(client, base_url, token).await {
            ProbeResult::Proven => return ReadinessOutcome::Ready,
            ProbeResult::Unproven => saw_unproven_response = true,
            ProbeResult::NoResponse => {}
        }
        if Instant::now() >= deadline {
            return if saw_unproven_response {
                ReadinessOutcome::IdentityMismatch
            } else {
                ReadinessOutcome::TimedOut
            };
        }
        tokio::time::sleep(interval).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::security::identity::compute_proof;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    const TOKEN: &str = "launch-token";

    /// How the fake server on the port answers `/health`.
    #[derive(Clone, Copy)]
    enum Behavior {
        /// Behaves like the real sidecar: valid HMAC proof.
        Genuine,
        /// 200 OK with no proof, like any unrelated local service.
        NoProof,
        /// Proof computed with a different token, like a guessing impostor.
        WrongProof,
    }

    /// Start a minimal HTTP server on a free loopback port; return its base URL.
    async fn fake_sidecar(behavior: Behavior) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    return;
                };
                let mut buf = vec![0u8; 4096];
                let n = sock.read(&mut buf).await.unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                let nonce = req
                    .lines()
                    .find_map(|l| {
                        let (k, v) = l.split_once(':')?;
                        k.eq_ignore_ascii_case(CHALLENGE_HEADER)
                            .then(|| v.trim().to_string())
                    })
                    .unwrap_or_default();
                let body = match behavior {
                    Behavior::Genuine => format!(
                        r#"{{"status":"ok","proof":"{}"}}"#,
                        compute_proof(TOKEN, &nonce)
                    ),
                    Behavior::NoProof => r#"{"status":"ok"}"#.to_string(),
                    Behavior::WrongProof => format!(
                        r#"{{"status":"ok","proof":"{}"}}"#,
                        compute_proof("not-the-token", &nonce)
                    ),
                };
                let resp = format!(
                    "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = sock.write_all(resp.as_bytes()).await;
                let _ = sock.shutdown().await;
            }
        });
        format!("http://{addr}")
    }

    fn client() -> reqwest::Client {
        reqwest::Client::builder()
            .connect_timeout(Duration::from_millis(200))
            .build()
            .unwrap()
    }

    async fn run(base: &str) -> ReadinessOutcome {
        wait_until_ready(
            &client(),
            base,
            TOKEN,
            Duration::from_millis(400),
            Duration::from_millis(50),
        )
        .await
    }

    #[tokio::test]
    async fn times_out_against_dead_port() {
        // Port 1 is privileged/closed; expect a prompt timeout.
        let outcome = wait_until_ready(
            &client(),
            "http://127.0.0.1:1",
            TOKEN,
            Duration::from_millis(150),
            Duration::from_millis(50),
        )
        .await;
        assert_eq!(outcome, ReadinessOutcome::TimedOut);
    }

    #[tokio::test]
    async fn genuine_sidecar_is_ready() {
        let base = fake_sidecar(Behavior::Genuine).await;
        assert_eq!(run(&base).await, ReadinessOutcome::Ready);
    }

    #[tokio::test]
    async fn service_without_proof_is_rejected() {
        let base = fake_sidecar(Behavior::NoProof).await;
        assert_eq!(run(&base).await, ReadinessOutcome::IdentityMismatch);
    }

    #[tokio::test]
    async fn service_with_wrong_proof_is_rejected() {
        let base = fake_sidecar(Behavior::WrongProof).await;
        assert_eq!(run(&base).await, ReadinessOutcome::IdentityMismatch);
    }
}
