# Contributing

## Local checks

Use a current pip when testing package metadata:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install -e .
python3 scripts/public_release_audit.py
python3 -m unittest discover -s tests -v
python3 -m pip wheel . --no-deps -w dist
```

Changes to OpenAPI, credential storage, configuration permissions, path
redaction, queue state, or merge output should include focused tests.

## Sensitive information

Do not commit or paste into an issue:

- AK/SK, tokens, cookies, SSH private keys, passwords, or signed URLs;
- personal usernames, personal home paths, or workstation absolute paths;
- source PDFs, parsed private documents, remote logs, or private deployment
  configuration.

Use uppercase placeholders such as `USER`, `EXAMPLE_USER`, `<access-key>`, and
`/path/to/document.pdf`. Run the all-history audit before making a previously
private branch public:

```bash
python3 scripts/public_release_audit.py --history
```

Report security issues through the private channel described in
[`SECURITY.md`](SECURITY.md), not a public issue.

## Compatibility

- Local CLI code supports Python 3.8 and newer.
- `src/ocrdrop/remote.py` must remain compatible with Python 3.6 because some
  login nodes use that interpreter.
- OpenAPI mutations must not be silently retried.
- SSH and OpenAPI transports must preserve the same batch and output contract.
