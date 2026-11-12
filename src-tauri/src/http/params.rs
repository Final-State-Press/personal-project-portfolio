//! Validation of renderer-supplied strings before they are placed in sidecar URLs.
//!
//! Typed identifiers (`i64`) cannot carry injection. Free-form strings such as
//! `week_key` and `status` can, so every such value passes through here: it is
//! validated against the shape the sidecar expects and then percent-encoded, so
//! it can never add query parameters or traverse to another route.

use crate::errors::{codes, CommandError};

/// Percent-encode a URL path segment or query value (RFC 3986 unreserved set).
pub fn encode_component(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for b in value.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn invalid(field: &str, message: &str) -> CommandError {
    let mut e = CommandError::new(codes::VALIDATION_ERROR, message);
    e.field_errors
        .insert(field.to_string(), vec![message.to_string()]);
    e
}

/// Validate a `YYYY.W` week key (four-digit year, one or two digit week).
#[allow(clippy::result_large_err)]
pub fn week_key(value: &str) -> Result<String, CommandError> {
    let ok = value.split_once('.').is_some_and(|(year, week)| {
        year.len() == 4
            && year.bytes().all(|b| b.is_ascii_digit())
            && (1..=2).contains(&week.len())
            && week.bytes().all(|b| b.is_ascii_digit())
    });
    if ok {
        Ok(encode_component(value))
    } else {
        Err(invalid("week_key", "week_key must look like 2026.15"))
    }
}

/// Validate a status filter: a short lowercase identifier such as `active`.
#[allow(clippy::result_large_err)]
pub fn status(value: &str) -> Result<String, CommandError> {
    let ok = !value.is_empty()
        && value.len() <= 32
        && value.bytes().all(|b| b.is_ascii_lowercase() || b == b'_');
    if ok {
        Ok(encode_component(value))
    } else {
        Err(invalid("status", "status must be a lowercase identifier"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_canonical_week_keys() {
        assert_eq!(week_key("2026.15").unwrap(), "2026.15");
        assert_eq!(week_key("2026.1").unwrap(), "2026.1");
    }

    #[test]
    fn rejects_traversal_and_injection_in_week_key() {
        for bad in [
            "..",
            "../projects",
            "2026.15&x=1",
            "2026.15/../x",
            "2026",
            "",
            "20x6.15",
            "2026.123",
        ] {
            assert!(week_key(bad).is_err(), "{bad:?} must be rejected");
        }
    }

    #[test]
    fn accepts_known_statuses() {
        for ok in ["active", "backlog", "archive"] {
            assert_eq!(status(ok).unwrap(), ok);
        }
    }

    #[test]
    fn rejects_query_injection_in_status() {
        for bad in ["active&week_key=1", "a b", "", "ACTIVE", "x#y", "x%26y"] {
            assert!(status(bad).is_err(), "{bad:?} must be rejected");
        }
    }

    #[test]
    fn encode_component_escapes_reserved_characters() {
        assert_eq!(
            encode_component("a&b=c/d?e#f g"),
            "a%26b%3Dc%2Fd%3Fe%23f%20g"
        );
        assert_eq!(encode_component("2026.15"), "2026.15");
    }
}
