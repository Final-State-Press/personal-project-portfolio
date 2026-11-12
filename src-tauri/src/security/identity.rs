//! Sidecar identity proof.
//!
//! The supervisor picks a free loopback port, releases it, and only then starts
//! the sidecar, so another local process could win the port. Before the token is
//! ever sent to whatever answers on that port, the supervisor sends a random
//! nonce to the unauthenticated `/health` route. Only a process that knows the
//! per-launch token can return `HMAC-SHA256(token, nonce)`. The nonce is public
//! and the proof reveals nothing about the token.
//!
//! The Python sidecar implements the matching side in
//! `portfolio_manager.api.routes.health`.

use hmac::{Hmac, Mac};
use rand::RngCore;
use sha2::Sha256;

/// Request header carrying the nonce.
pub const CHALLENGE_HEADER: &str = "X-Challenge";

/// Number of random bytes in a challenge nonce.
pub const NONCE_BYTES: usize = 16;

type HmacSha256 = Hmac<Sha256>;

fn to_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn from_hex(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 || !s.is_ascii() {
        return None;
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).ok())
        .collect()
}

/// Generate a fresh random nonce as lowercase hex.
pub fn generate_nonce() -> String {
    let mut bytes = [0u8; NONCE_BYTES];
    rand::thread_rng().fill_bytes(&mut bytes);
    to_hex(&bytes)
}

/// The proof the real sidecar returns for `nonce`: hex `HMAC-SHA256(token, nonce)`.
pub fn compute_proof(token: &str, nonce: &str) -> String {
    let mut mac = HmacSha256::new_from_slice(token.as_bytes()).expect("HMAC accepts any key size");
    mac.update(nonce.as_bytes());
    to_hex(&mac.finalize().into_bytes())
}

/// Verify a proof in constant time. Malformed hex is simply invalid.
pub fn verify_proof(token: &str, nonce: &str, proof_hex: &str) -> bool {
    let Some(proof) = from_hex(proof_hex) else {
        return false;
    };
    let mut mac = HmacSha256::new_from_slice(token.as_bytes()).expect("HMAC accepts any key size");
    mac.update(nonce.as_bytes());
    mac.verify_slice(&proof).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nonce_is_random_hex_of_expected_length() {
        let a = generate_nonce();
        assert_eq!(a.len(), NONCE_BYTES * 2);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, generate_nonce());
    }

    #[test]
    fn valid_proof_verifies() {
        let proof = compute_proof("secret-token", "abc123");
        assert!(verify_proof("secret-token", "abc123", &proof));
    }

    #[test]
    fn wrong_token_nonce_or_garbage_fails() {
        let proof = compute_proof("secret-token", "abc123");
        assert!(!verify_proof("other-token", "abc123", &proof));
        assert!(!verify_proof("secret-token", "different", &proof));
        assert!(!verify_proof("secret-token", "abc123", ""));
        assert!(!verify_proof("secret-token", "abc123", "zz"));
        assert!(!verify_proof("secret-token", "abc123", "é"));
        assert!(!verify_proof(
            "secret-token",
            "abc123",
            &proof[..proof.len() - 2]
        ));
    }

    /// Fixed vector shared with the Python test so both sides stay in agreement.
    #[test]
    fn matches_python_reference_vector() {
        assert_eq!(
            compute_proof("test-token-123", "00112233445566778899aabbccddeeff"),
            "b72369d397d74cff8ca0f73b6e02313b00c8463c247fca73270aead986734781"
        );
    }
}
