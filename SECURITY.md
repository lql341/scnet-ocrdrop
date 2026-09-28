# Security

## Sensitive files

Do not commit:

- real cluster configuration;
- SSH keys, passwords, access tokens, or cookies;
- AK/SK credentials, region tokens, or credential-bearing URLs;
- personal usernames, personal home paths, or workstation absolute paths;
- source PDFs or parsed document output;
- proprietary model files.

Put real deployment profiles and launchers under `config.local/`, which is
ignored by Git.

`scnet-ocrdrop setup` stores only non-secret selections under
`$XDG_CONFIG_HOME/scnet-ocrdrop/` or `~/.config/scnet-ocrdrop/`. The directory
uses mode `0700` and configuration files use mode `0600`.

OpenAPI AK/SK credentials are stored in macOS Keychain or Linux Secret Service.
When neither is available, inject them through `SCNET_OPENAPI_USER`,
`SCNET_OPENAPI_ACCESS_KEY`, and `SCNET_OPENAPI_SECRET_KEY`. Do not put these
variables in committed shell scripts or `.env` files.

Command output redacts personal absolute paths by default. `--show-paths` is an
explicit local diagnostic option; do not use its output in public issues.

## Reporting

For a security issue, contact the repository owner privately. Do not open a
public issue containing credentials, internal infrastructure details, source
documents, or remote logs.
