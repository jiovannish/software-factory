# GitHub tasks with Jio

One task creates one Jio VM, clones your repository, runs your choice of Codex or Claude, checks the changes, opens a draft PR, and destroys the VM. GitHub Actions supplies the orchestration and logs. No persistent service or dashboard is required.

This uses the request-to-agent-to-human-handoff pattern described by [Warp Factories](https://docs.warp.dev/factories/how-factories-work/). The first version chooses one agent per task and runs a fixed workflow.

## Setup

Start with a public GitHub repository whose default branch contains the code to change.

1. Copy [`examples/jio.yml`](../examples/jio.yml) to `.github/workflows/jio.yml` in your repository. Replace both `REPLACE_WITH_FACTORY_COMMIT` values with the **same full commit SHA** from this factory repository. The reusable workflow and its Python implementation must use the same reviewed revision. If you fork the factory itself, change the caller's `uses` repository and pass `factory_repository` too.
2. Copy [`examples/factory.toml`](../examples/factory.toml) to `factory.toml` in your repository. Set your setup command, test command, and the model ID for each provider you will use. Model IDs are explicit; the factory does not select a model or fall back to another provider. Commands run from the cloned repository using `bash -e -o pipefail`.
3. Ask your Jio operator for a **dedicated account for this repository** with the resources below. Do not reuse a personal account or the deployment example's account.
4. Add `JIO_API_KEY` and `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or both under **Settings → Secrets and variables → Actions**. Only the selected provider key is needed for a task. Use dedicated provider API keys, not copied personal CLI logins.
5. Under **Settings → Actions → General**, allow GitHub Actions to create pull requests. Organization policy must also allow this setting and the workflow's actions. Keep required human review and branch protection on your default branch.

| Account setting | Requirement |
| --- | --- |
| CPU quota | 2 vCPU |
| RAM quota | 4,096 MiB |
| Disk quota | At least 65,792 MiB |
| Session expiry | Exactly 3,600 seconds from VM creation |
| Profile | Medium, the hosted endpoint's default |
| Availability | One free worker slot |

Before creating a VM, the workflow verifies the account expiry, quotas, and zero existing reservations. An occupied account fails preflight; the factory never destroys its previous or unrelated VMs to make room.

The guest template must provide Python 3.12, Node.js 24/npm, Git, Bash, OpenSSH, and the `jio` user with home `/home/jio`. Add your language-specific setup to `factory.toml`; for example, Python projects may need `sudo apt-get install -y python3.12-venv`. No template promotion or Core/Server code change is needed to start.

The selected agent is installed into the guest's home directory at a pinned version: Codex **0.154.0**, Claude Code **2.1.81**. Dependencies and agent credentials are not baked into a reusable template. Baking the public tooling can be a later optimization.

For an explicitly approved personal Codex test, `CODEX_AUTH_JSON` can temporarily hold a copy of your Codex `auth.json` instead of `OPENAI_API_KEY`. An API key takes precedence when both exist. This login can include refresh tokens: treat the whole file as a credential, remove the temporary GitHub secret after testing, and do not publish it in logs or artifacts. The guest writes it outside the checkout with mode `0600`; VM destruction removes the copy. Refreshed login state is not synchronized back to your machine or GitHub. Dedicated provider API keys remain the supported default for ongoing automation.

## Assign a task

Anyone can open an issue. Opening it does not allocate a VM or run an agent: the ticket waits for **saugardev** to approve it. Approve and select the agent by commenting:

```text
/jio codex
Also cover names containing only whitespace.
```

Use `/jio claude` to choose Claude. The command must occupy the first line; following lines add task context to the issue title and body. Only a command from saugardev starts the task, including on somebody else's issue. Other users cannot start agents, including other repository administrators. Repository write permission is still required for saugardev.

Alternatively, create and apply `jio:codex` or `jio:claude` labels, or select **Actions → Jio task → Run workflow** and supply an open issue number and agent. Manual runs must use the trusted default branch. The same saugardev-only restriction applies to labels, manual runs and reruns, including reruns of failed jobs. Bot requests, edited comments, and comments on pull requests are ignored.

The factory freezes the base commit, issue context, and configuration before starting. It respects repository instructions and lets the selected agent edit files and run tools in the VM. After the agent finishes, the workflow runs your configured test command again.

Successful runs produce a draft PR with an agent summary and a link to the workflow evidence. Failed tests, failed agent execution, or an empty patch produce no PR. Inspect the failure, then issue another command for a fresh task. There is no automatic repair loop.

The factory's normal Checks workflow runs offline tests, workflow linting, and a Linux SDK build/import check. It does not call a model, create VMs, or need secrets.

## Results and publication

- The issue receives a completion or failure comment once an authorized task reaches the VM job.
- Workflow artifacts retain the frozen task, test output, outcome, and successful patch for **seven days**. Raw agent transcripts, provider credentials and SSH keys are not uploaded.
- The PR is created by `github-actions[bot]`. Its branch is `jio/issue-<number>-<run-id>`. Re-running the same successful workflow recognizes its existing PR; a new comment represents a new task.
- Publication never overwrites an existing branch with different code. If pushing succeeds but PR creation fails, the branch stays available for inspection or a manual draft PR. A retry can reuse an identical commit.
- The built-in GitHub token needs `contents: write`, `issues: write`, and `pull-requests: write` only in the publishing job. There is no `REPO_ADMIN_TOKEN` or GitHub App requirement.
- GitHub's separate PR workflows created with this token require a maintainer to select **Approve workflows to run**. Tests inside Jio have already run. [GitHub documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)

Patches are capped at **4 MiB**. Changes to `factory.toml`, `.github`, `.gitmodules`, and `.gitattributes` are rejected. Configure or change the automation itself through an ordinary human-reviewed PR.

## Lifecycle and recovery

Runs are serialized per target repository using `cancel-in-progress: false`. GitHub can replace an older **pending** run with a newer one; this is not a durable FIFO queue. Separate target repositories need separate accounts and sufficient worker capacity.

The agent has at most **30 minutes**. The VM job has a **45-minute** timeout, including SDK setup and cleanup. Guest operations share a 40-minute deadline; each setup/test command is capped at ten minutes. The SDK bounds SSH command output to 8 MiB. Overlong or excessively noisy commands fail the task.

Normal completion, command failure, and handled cancellation all attempt to destroy the recorded VM. An `always()` step retries cleanup. Local SSH state is then removed. Only the recorded VM ID is eligible for cleanup.

If the create response is lost, the Python SDK does not expose the VM ID early enough for this workflow to reconcile it safely. The run stops without retrying creation or deleting other VMs. Account expiry is the fallback for this case, runner loss, and unreachable cleanup. Expiry enforcement is periodic and cannot guarantee immediate reclamation during a control-plane or worker outage. An operator must resolve reservations that remain after service recovery.

GitHub publishing failures do not retain the VM. Test evidence and any pushed factory branch remain available until their normal retention or manual deletion.

## Security boundary

Codex runs with full guest access and no interactive approvals. The disposable Jio VM supplies isolation; Codex's nested Bubblewrap sandbox cannot pivot the current guest root filesystem. Run this guest helper only inside the dedicated task VM. The agent can modify the whole guest, and VM destruction is required after every task.

GitHub and Jio credentials stay on the GitHub runner. Source is cloned anonymously inside the VM at the frozen commit. The selected provider key is sent through the SDK's pinned SSH transport on stdin, and supplied only to the agent invocation. Setup and final test invocations receive no provider key from the orchestrator. [Codex automation](https://learn.chatgpt.com/docs/non-interactive-mode) and [Claude programmatic usage](https://code.claude.com/docs/en/headless) describe their native noninteractive interfaces.

The publishing job receives no Jio or model credentials. It applies the patch with Git hooks and external configuration disabled, then pushes without executing the target repository's scripts or dependencies. The patch digest must match the artifact recorded after tests.

This is intended for **maintainer-approved repository code on a trusted Jio host**. The selected agent and code running inside its VM may access the provider credential. KVM does not hide VM contents from the host operator. Secret redaction is a backstop, not protection against deliberate encoded exfiltration. Tests run in the agent's VM; they are evidence, not independent attestation that the guest or agent behaved honestly.

## Local development

Python 3.12 and Git are sufficient for offline tests:

```sh
python3.12 -m unittest discover -s tests -v
actionlint .github/workflows/*.yml examples/jio.yml
```

Tests exercise real Git patch application, branch publication logic with a fake remote, both agent output formats, event authorization, command failures, timeouts, secret handling, and VM cleanup. [`tests/fixture`](../tests/fixture) is a tiny Python repository used for local acceptance. Model responses, GitHub requests and VM operations are faked; passing these tests does not prove a live agent run.

The workflow builds the Jio Python SDK from client commit `8b243ef86aa45d889c3aec5dd225504c18081edd`, using Rust 1.94.0 and Maturin 1.15.0, and caches the Python 3.12 wheel. SDKs are currently distributed as source. VM operations use this SDK; the quota preflight reads the existing `/v1/usage` route with the public CA from the same pinned client source because Python does not yet bind `usage`.

To build the SDK locally, follow the [client source instructions](https://github.com/jiovannish/client/tree/v0.2.0/bindings/python). Set `JIO_CA_CERT` to that checkout's `crates/jio-client/src/jio-ca.pem` when using the orchestrator.

## Live acceptance

Use a disposable public repository and dedicated factory account. Live acceptance spends model credits and allocates VMs.

1. Configure the caller and a testable Python fixture. Open an issue asking to trim whitespace in the greeting and add a regression test.
2. Assign it with `/jio codex`. Verify the base commit, changes, passing test evidence, draft status, issue comment, and destroyed VM.
3. Repeat with `/jio claude` on another issue. Confirm the selected provider actually handled each task.
4. Re-run a completed workflow and verify it creates no second PR or VM.
5. Set the test command to fail in the disposable repository, assign a new task, and verify failure reporting, no draft PR, and VM cleanup.
6. Cancel a running task and confirm cleanup or eventual account expiry. Inspect reservations from a trusted machine using `jio usage` and `jio list` with the dedicated account.
7. Try an unauthorized comment and a request without its selected provider secret; confirm neither creates a VM.

## Limitations

Public GitHub repositories and GitHub-hosted Ubuntu runners only. No private-repository authentication, persistent workspace, public app preview, automatic merge, multi-agent team, custom dashboard, durable task queue, or VM/host restart recovery. A new task starts from scratch. The factory does not enforce a cross-provider dollar budget; configure provider account spending controls and inspect usage there.

This repository includes its own caller and test configuration. Creating a ticket still waits for saugardev approval, and live execution requires Jio and provider credentials. Installing the factory on another repository requires the setup above. Jio remains experimental.
