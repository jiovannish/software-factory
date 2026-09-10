# Jio Software Factory

Give a GitHub issue to Codex or Claude. It works inside a fresh Jio VM and returns a tested draft PR.

[Documentation](docs/README.md) · [Contributing](CONTRIBUTING.md)

## Getting started

[Configure your repository](docs/README.md#setup), then comment on an issue:

```text
/jio codex
```

Or choose Claude:

```text
/jio claude
```

GitHub Actions coordinates the job. Jio runs the agent and tests. You review and merge; the VM is destroyed after the task.

Public repositories only. Jio is experimental; read the [limitations](docs/README.md#limitations).

## Security

Report vulnerabilities privately using our [security policy](SECURITY.md).

## License

[Apache-2.0](LICENSE).
