# Changelog

## 0.4.0 - 2026-09-28

- Add SCNet OpenAPI authentication, region discovery, file transfer, and
  control-job transport.
- Add `setup new|modify|status|reset` configuration lifecycle.
- Store AK/SK in macOS Keychain or Linux Secret Service, with environment
  injection as the non-persistent fallback.
- Store only non-secret, home-relative metadata in the XDG configuration
  directory with `0700/0600` permissions.
- Keep the existing SSH/SCP transport and Kunshan compatibility wrappers.
- Redact credentials and personal absolute paths from command output by
  default.
- Remove source paths, output paths, traceback paths, and worker hostnames from
  fetched result manifests.
- Add wheel build/install validation and third-party notices.
- Add a CI public-release audit for credentials and personal absolute paths,
  with an optional all-history mode.
