"""Offline acceptance: real Git and subprocesses; fake GitHub, Jio and model calls."""
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import factory, guest

CONFIG = 'setup="true"\ntest="python3 -m unittest discover -v"\n[models]\ncodex="test-model"\nclaude="test-model"\n'
PATCH = b'''diff --git a/greeting.py b/greeting.py
--- a/greeting.py
+++ b/greeting.py
@@ -1,2 +1,2 @@
 def greet(name):
-    return f"Hello, {name}!"
+    return f"Hello, {name.strip()}!"
'''


def task(agent="codex"):
    return {"repository": "example/project", "issue": 7, "agent": agent,
            "title": "Trim the greeting", "body": "Strip surrounding whitespace from names.",
            "comment": "", "base_sha": "a" * 40, "base_branch": "main", "run_id": "42",
            "branch": "jio/issue-7-42", "config": factory.config_from_toml(CONFIG, agent)}


class Result:
    def __init__(self, output=b"", exit_code=0):
        self.stdout, self.stderr = output, b""
        self.exit_code, self.success = exit_code, exit_code == 0
        self.stdout_text, self.stderr_text = output.decode(), ""

    def raise_for_status(self):
        if not self.success:
            raise RuntimeError("Guest failed")


class FakeJio:
    def __init__(self, failure=None):
        self.id = "b" * 32
        self.failure = failure
        self.operations, self.destroyed = [], []
        self.created = 0
        self.patch = PATCH

    def create(self):
        self.created += 1
        if self.failure == "create":
            raise TimeoutError("Create response lost")
        return self

    def write_file(self, path, contents, timeout):
        assert contents and path == factory.GUEST

    def exec(self, command, *, input=None, timeout):
        if input is None:
            return Result()
        operation, data = command.split()[-1], json.loads(input)
        self.operations.append((operation, data, timeout))
        if self.failure == operation:
            return Result(b"failed check\n", 1)
        if self.failure == "timeout" and operation == "agent":
            raise TimeoutError("SSH timed out")
        if operation == "agent":
            assert data["key"] == "test-provider-secret"
            if data["agent"] == "claude":
                return Result(json.dumps({"subtype": "success", "result": "Fixed greeting.", "is_error": False}).encode())
            return Result(b'{"type":"item.completed","item":{"type":"agent_message","text":"Fixed greeting."}}\n{"type":"turn.completed"}\n')
        if operation == "patch":
            return Result(self.patch)
        return Result(b"checks passed\n")

    def destroy(self, vm_id):
        self.destroyed.append(vm_id)
        if self.failure == "cleanup":
            raise ConnectionError("Jio unavailable")


class FactoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output, self.state = self.root / "out", self.root / "state.json"
        self.env = {"GITHUB_REPOSITORY": "example/project", "GITHUB_EVENT_NAME": "issue_comment",
                    "GITHUB_RUN_ID": "42", "GITHUB_ACTOR": "maintainer"}
        self.enterContext(patch.dict(os.environ, self.env))
        self.event = {"repository": {"full_name": "example/project"}, "action": "created",
                      "sender": {"type": "User"}, "issue": {"number": 7, "title": "Trim greeting",
                      "body": "Fix names", "state": "open"}, "comment": {"body": "/jio codex\nTrim names."}}

    def api(self, path):
        if path.endswith("/permission"):
            return {"permission": "write"}
        if "/contents/" in path:
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(CONFIG.encode()).decode()}
        if "/commits/" in path:
            return {"sha": "a" * 40}
        return {"private": False, "default_branch": "main"}

    def run_work(self, client, chosen="codex", key="test-provider-secret"):
        with redirect_stdout(io.StringIO()):
            return factory.work(task(chosen), client, self.output, self.state,
                                key, "test-jio-secret", preflight=lambda *_: None)

    def test_both_agents_complete_the_same_lifecycle(self):
        for chosen in ("codex", "claude"):
            with self.subTest(agent=chosen):
                client = FakeJio()
                result = self.run_work(client, chosen)
                self.assertTrue(result["passed"])
                self.assertEqual(result["cleanup"], "destroyed")
                self.assertEqual(client.destroyed, [client.id])
                self.assertEqual([op[0] for op in client.operations],
                                 ["clone", "setup", "install", "agent", "patch", "test", "patch"])
                self.assertEqual((self.output / "change.patch").read_bytes(), PATCH)
                for operation, payload, timeout in client.operations:
                    self.assertLessEqual(timeout, 1800)
                    self.assertEqual("key" in payload, operation == "agent")
                for file in self.output.iterdir():
                    self.assertNotIn(b"test-provider-secret", file.read_bytes())
                    self.assertNotIn(b"test-jio-secret", file.read_bytes())

    def test_failed_steps_and_timeout_destroy_the_vm(self):
        for failure in ("setup", "install", "agent", "test", "timeout"):
            with self.subTest(failure=failure):
                client = FakeJio(failure)
                result = self.run_work(client)
                self.assertFalse(result["passed"])
                self.assertEqual(client.destroyed, [client.id])

    def test_uncertain_create_does_not_retry_or_delete_other_vms(self):
        client = FakeJio("create")
        result = self.run_work(client)
        self.assertEqual(client.created, 1)
        self.assertEqual(client.destroyed, [])
        self.assertIn("unknown", result["cleanup"])
        self.assertTrue(factory.read_json(self.state)["create_attempted"])

    def test_cleanup_is_retryable_and_idempotent(self):
        client = FakeJio("cleanup")
        result = self.run_work(client)
        self.assertIn("failed", result["cleanup"])
        client.failure = None
        factory.cleanup(client, self.state)
        factory.cleanup(client, self.state)
        self.assertEqual(client.destroyed, [client.id, client.id])

    def test_missing_secret_does_not_create_a_vm(self):
        client = FakeJio()
        self.assertFalse(self.run_work(client, key="")["passed"])
        self.assertEqual(client.created, 0)

    def test_empty_oversized_or_secret_patch_is_rejected(self):
        for value in (b"", b"x" * (factory.MAX_PATCH + 1), PATCH + b"test-provider-secret"):
            client = FakeJio()
            client.patch = value
            self.assertFalse(self.run_work(client)["passed"])
            self.assertEqual(client.destroyed, [client.id])

    def test_tests_cannot_change_the_published_candidate(self):
        client = FakeJio()
        original = client.exec
        def changed(command, **kwargs):
            result = original(command, **kwargs)
            if command.endswith(" test"):
                client.patch += b"changed during tests"
            return result
        client.exec = changed
        self.assertFalse(self.run_work(client)["passed"])

    def test_authorization_freezes_context_and_commit(self):
        result = factory.authorize(self.event, self.env, self.api)
        self.assertEqual(result["agent"], "codex")
        self.assertEqual(result["base_sha"], "a" * 40)
        self.assertEqual(result["comment"], "Trim names.")
        self.event.update(action="labeled", label={"name": "jio:claude"})
        self.env["GITHUB_EVENT_NAME"] = "issues"
        self.assertEqual(factory.authorize(self.event, self.env, self.api)["agent"], "claude")

    def test_artifacts_must_belong_to_this_repository_and_run(self):
        for values in ({"GITHUB_REPOSITORY": "different/project"}, {"GITHUB_RUN_ID": "99"}):
            with patch.dict(os.environ, values), self.assertRaises(ValueError):
                factory.validate_task(task())

    def test_manual_run_selects_agent_and_issue(self):
        result = factory.request_from_event(self.event, "workflow_dispatch", {"agent": "claude", "issue": "7"})
        self.assertEqual(result, ("claude", 7, ""))
        self.env.update(GITHUB_EVENT_NAME="workflow_dispatch", REQUEST_AGENT="claude",
                        REQUEST_ISSUE="7", GITHUB_REF="refs/heads/unreviewed")
        with self.assertRaises(factory.Skip):
            factory.authorize(self.event, self.env, self.api)

    def test_untrusted_or_irrelevant_events_are_ignored(self):
        for change in ({"action": "edited"}, {"sender": {"type": "Bot"}},
                       {"comment": {"body": "please /jio codex"}},
                       {"issue": {"number": 7, "pull_request": {}}}):
            event = self.event | change
            with self.assertRaises(factory.Skip):
                factory.request_from_event(event, "issue_comment", {})
        def denied(path):
            return {"permission": "read"}
        with self.assertRaises(factory.Skip):
            factory.authorize(self.event, self.env, denied)
        self.env["GITHUB_TRIGGERING_ACTOR"] = "outsider"
        def rerun(path):
            return {"permission": "read"} if "/outsider/" in path else self.api(path)
        with self.assertRaises(factory.Skip):
            factory.authorize(self.event, self.env, rerun)

    def test_malformed_config_and_command_inputs_fail_closed(self):
        for config in (CONFIG.replace('test="python3 -m unittest discover -v"', 'test=""'),
                       CONFIG.replace('"test-model"', '"REPLACE_MODEL"'), CONFIG + "\nunknown=1"):
            with self.assertRaises(ValueError):
                factory.config_from_toml(config, "codex")
        for inputs in ({"agent": "$(id)", "issue": "7"}, {"agent": "claude", "issue": "7;id"}):
            with self.assertRaises(ValueError):
                factory.request_from_event(self.event, "workflow_dispatch", inputs)

    def test_expiry_and_account_reservations_are_checked(self):
        usage = {"session_ttl_seconds": 3600, "limits": {"cpu": 2, "memory_mib": 4096, "disk_mib": 65792},
                 "reserved": {"cpu": 0, "memory_mib": 0, "disk_mib": 0}}
        client = type("Client", (), {"endpoint": "https://example.com"})()
        with patch.dict(os.environ, {"JIO_CA_CERT": "unused"}), patch.object(factory.ssl, "create_default_context"), patch.object(factory, "request_json", return_value=usage):
            factory.preflight_account(client, "unused")
            usage["session_ttl_seconds"] = None
            with self.assertRaises(ValueError):
                factory.preflight_account(client, "unused")
            usage["session_ttl_seconds"] = 3600
            usage["reserved"]["cpu"] = 2
            with self.assertRaises(ValueError):
                factory.preflight_account(client, "unused")

    def test_provider_failures_are_not_treated_as_success(self):
        for agent, data in (("codex", '{"type":"turn.failed"}'),
                            ("claude", '{"subtype":"error","is_error":true}')):
            with self.assertRaises(RuntimeError):
                factory.agent_result(agent, data)
        self.assertEqual(factory.scrub("::error::\x1b[31msecret", ["secret"]), ": :error: :[REDACTED]")

    def init_git(self):
        repo = self.root / "repo"
        shutil.copytree(Path(__file__).parent / "fixture", repo)
        env = factory.git_environment(self.root)
        factory.git(["init", "-q"], repo, env)
        factory.git(["add", "."], repo, env)
        factory.git(["commit", "-qm", "fixture"], repo, env)
        return repo, env

    def test_real_patch_and_project_checks(self):
        repo, env = self.init_git()
        file = self.root / "change.patch"
        file.write_bytes(PATCH)
        factory.apply_patch(file, repo, env)
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"], cwd=repo, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, "-c", "from greeting import greet; assert greet(' Jio ')== 'Hello, Jio!'"], cwd=repo, check=False)
        self.assertEqual(result.returncode, 0)

    def test_control_file_edits_and_bad_patches_are_rejected(self):
        repo, env = self.init_git()
        file = self.root / "change.patch"
        file.write_bytes(b"not a patch")
        with self.assertRaises(RuntimeError):
            factory.apply_patch(file, repo, env)
        file.write_text("diff --git a/../escaped b/../escaped\nnew file mode 100644\n--- /dev/null\n+++ b/../escaped\n@@ -0,0 +1 @@\n+bad\n")
        with self.assertRaises(RuntimeError):
            factory.apply_patch(file, repo, env)
        self.assertFalse((self.root / "escaped").exists())
        file.write_text("diff --git a/factory.toml b/factory.toml\nnew file mode 100644\n--- /dev/null\n+++ b/factory.toml\n@@ -0,0 +1 @@\n+test='true'\n")
        with self.assertRaises(ValueError):
            factory.apply_patch(file, repo, env)

    def test_guest_exports_new_files_and_agent_commits(self):
        repo, env = self.init_git()
        base = factory.git(["rev-parse", "HEAD"], repo, env).decode().strip()
        (repo / "greeting.py").write_text('def greet(name):\n    return f"Hello, {name.strip()}!"\n')
        factory.git(["commit", "-am", "Agent committed a change"], repo, env)
        (repo / "new_file.py").write_text("NEW = True\n")
        repo.rename(self.root / "software-factory-task")
        result = subprocess.run([sys.executable, str(factory.SOURCE / "guest.py"), "patch"],
                                input=json.dumps({"base_sha": base}).encode(),
                                capture_output=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"name.strip()", result.stdout)
        self.assertIn(b"new file mode", result.stdout)
        self.assertIn(b"+NEW = True", result.stdout)

    def test_guest_does_not_forward_control_plane_credentials(self):
        payload = {"agent": "claude", "model": "test-model", "key": "selected-key", "prompt": "Do the task"}
        with patch.dict(os.environ, {"GH_TOKEN": "github-key", "JIO_API_KEY": "jio-key"}), patch.object(sys, "argv", ["guest.py", "agent"]), patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), patch.object(guest.subprocess, "run") as run:
            run.return_value.returncode = 0
            with self.assertRaises(SystemExit) as exit:
                guest.main()
            self.assertEqual(exit.exception.code, 0)
            env = run.call_args.kwargs["env"]
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("JIO_API_KEY", env)
            self.assertEqual(env["ANTHROPIC_API_KEY"], "selected-key")
            self.assertNotIn("selected-key", str(run.call_args.args))

    def test_publisher_creates_a_draft_without_running_candidate_code(self):
        repo, env = self.init_git()
        item = task()
        item["base_sha"] = factory.git(["rev-parse", "HEAD"], repo, env).decode().strip()
        self.output.mkdir()
        (self.output / "change.patch").write_bytes(PATCH)
        factory.write_json(self.output / "outcome.json", {"passed": True, "cleanup": "destroyed",
                           "patch_sha256": hashlib.sha256(PATCH).hexdigest()})
        calls = []
        def api(path, method="GET", body=None):
            if "pulls?" in path:
                return []
            if "/git/ref/" in path:
                raise urllib.error.HTTPError("https://example.com", 404, "missing", {}, None)
            calls.append((path, method, body))
            return {"html_url": "https://github.com/example/project/pull/8"}
        original = factory.git
        def local_git(command, directory, environ):
            self.assertNotIn("test-publish-token", " ".join(command))
            if command[0] == "fetch":
                command = ["fetch", str(repo), item["base_sha"]]
            if command[0] == "push":
                self.assertIn("--force-with-lease=refs/heads/jio/issue-7-42:", command)
                self.assertIn("GIT_CONFIG_VALUE_0", environ)
                return b""
            return original(command, directory, environ)
        with patch.dict(os.environ, {"GH_TOKEN": "test-publish-token"}), patch.object(factory, "git", side_effect=local_git):
            url = factory.publish(item, self.output, api)
        self.assertTrue(url.endswith("/8"))
        self.assertTrue(calls[0][2]["draft"])
        self.assertEqual(calls[0][2]["head"], item["branch"])
        with patch.object(factory, "git", side_effect=AssertionError("Duplicate publish")):
            url = factory.publish(item, self.output, lambda *args: [{"html_url": url}])
        (self.output / "change.patch").write_bytes(PATCH + b"tampered")
        with self.assertRaises(ValueError):
            factory.publish(item, self.output, api)

    def test_guest_uses_native_agent_clis_and_checks_shell_failures(self):
        for agent, binary in (("codex", "codex"), ("claude", "claude")):
            command = guest.agent_command(agent, "a-model")
            self.assertEqual(command[0], binary)
            self.assertIn("a-model", command)
            self.assertFalse(any("bypass" in part or "skip-permissions" in part for part in command))
        # A pipeline's final successful command cannot hide an earlier failed check.
        with (
            patch.object(guest, "ROOT", self.root),
            patch.object(sys, "argv", ["guest.py", "test"]),
            patch.object(sys, "stdin", io.StringIO('{"command":"false | true"}')),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            guest.main()


if __name__ == "__main__":
    unittest.main()
