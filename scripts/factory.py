"""GitHub coordinates one task; one selected coding agent works inside Jio."""
import base64
import hashlib
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import tomllib

MAX_PATCH = 4 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
GUEST = "/home/jio/.software-factory/guest.py"
SOURCE = Path(__file__).resolve().parent
AGENTS = {"codex": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY"}
ALLOWED_USER = "saugardev"


class Skip(Exception):
    """An event that must not allocate compute or receive credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Refusing an authenticated HTTP redirect")


def request_json(url, token, method="GET", body=None, context=None):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Authenticated requests require HTTPS")
    request = urllib.request.Request(url, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "Content-Type": "application/json", "User-Agent": "jio-software-factory"})
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    with opener.open(request, timeout=30) as response:
        payload = response.read(MAX_JSON + 1)
    if len(payload) > MAX_JSON:
        raise ValueError("API response exceeds 2 MiB")
    return json.loads(payload) if payload else None


def github(path, method="GET", body=None):
    return request_json("https://api.github.com/" + path, os.environ["GH_TOKEN"], method, body)


def require(pattern, value, name):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid {name}")
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def read_json(path):
    if path.stat().st_size > MAX_JSON:
        raise ValueError("JSON file exceeds 2 MiB")
    return json.loads(path.read_text())


def request_from_event(event, name, inputs):
    if event.get("sender", {}).get("type") == "Bot":
        raise Skip("Bot request")
    issue = event.get("issue", {})
    if "pull_request" in issue:
        raise Skip("Only issues are supported")
    comment = ""
    if name == "issue_comment" and event.get("action") == "created":
        body = event.get("comment", {}).get("body", "")
        lines = body.strip().splitlines()
        match = re.fullmatch(r"/jio (codex|claude)", lines[0]) if lines else None
        if not match:
            raise Skip("Not a factory command")
        agent, number, comment = match[1], issue.get("number"), "\n".join(lines[1:])
    elif name == "issues" and event.get("action") == "labeled":
        label = event.get("label", {}).get("name", "")
        if label not in {"jio:codex", "jio:claude"}:
            raise Skip("Not a factory label")
        agent, number = label.split(":")[1], issue.get("number")
    elif name == "workflow_dispatch":
        agent, number = inputs.get("agent"), inputs.get("issue")
    else:
        raise Skip("Unsupported event")
    if agent not in AGENTS:
        raise ValueError("Choose codex or claude")
    number = require(r"[1-9][0-9]{0,9}", str(number), "issue number")
    return agent, int(number), comment


def config_from_toml(text, agent):
    config = tomllib.loads(text)
    if set(config) != {"setup", "test", "models"}:
        raise ValueError("factory.toml requires only setup, test, and [models]")
    for key in ("setup", "test"):
        if not isinstance(config[key], str) or not config[key].strip() or len(config[key]) > 8192:
            raise ValueError(f"Configure a nonempty {key} command (up to 8 KiB)")
    if not isinstance(config["models"], dict) or set(config["models"]) - AGENTS.keys():
        raise ValueError("models supports codex and claude only")
    require(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", config["models"].get(agent), "selected model")
    if config["models"][agent].startswith("REPLACE_"):
        raise ValueError("Set the selected provider's model ID in factory.toml")
    return config


def validate_task(task):
    require(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", task["repository"], "repository")
    require(r"[a-f0-9]{40}", task["base_sha"], "base commit")
    require(r"[1-9][0-9]*", str(task["issue"]), "issue")
    require(r"[1-9][0-9]*", task["run_id"], "run ID")
    if task["agent"] not in AGENTS or task["branch"] != f"jio/issue-{task['issue']}-{task['run_id']}":
        raise ValueError("Invalid task agent or branch")
    if os.environ.get("GITHUB_REPOSITORY", task["repository"]) != task["repository"]:
        raise ValueError("Task belongs to another repository")
    if os.environ.get("GITHUB_RUN_ID", task["run_id"]) != task["run_id"]:
        raise ValueError("Task belongs to another workflow run")


def existing_pr(task, api=github):
    query = urllib.parse.urlencode({"state": "all", "head": task["repository"].split("/")[0] + ":" + task["branch"]})
    pulls = api(f"repos/{task['repository']}/pulls?{query}")
    return pulls[0] if pulls else None


def authorize(event, env, api=github):
    if any(env.get(key, env.get("GITHUB_ACTOR")) != ALLOWED_USER
           for key in ("GITHUB_ACTOR", "GITHUB_TRIGGERING_ACTOR")):
        raise Skip("Only saugardev can launch factory tasks")
    agent, issue_number, comment = request_from_event(event, env["GITHUB_EVENT_NAME"],
        {"agent": env.get("REQUEST_AGENT"), "issue": env.get("REQUEST_ISSUE")})
    repo = require(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", env["GITHUB_REPOSITORY"], "repository")
    if event.get("repository", {}).get("full_name") != repo:
        raise ValueError("Event repository mismatch")
    for actor in {env["GITHUB_ACTOR"], env.get("GITHUB_TRIGGERING_ACTOR", env["GITHUB_ACTOR"])}:
        actor = require(r"[A-Za-z0-9_-]+", actor, "actor")
        access = api(f"repos/{repo}/collaborators/{actor}/permission")
        if access.get("permission") not in {"write", "maintain", "admin"}:
            raise Skip("Repository write permission is required")
    metadata = api(f"repos/{repo}")
    if metadata.get("private") is not False:
        raise ValueError("This version supports public repositories only")
    issue = event.get("issue") or api(f"repos/{repo}/issues/{issue_number}")
    if "pull_request" in issue or issue.get("state") != "open":
        raise Skip("An open issue is required")
    branch = metadata["default_branch"]
    if env["GITHUB_EVENT_NAME"] == "workflow_dispatch" and env.get("GITHUB_REF") != f"refs/heads/{branch}":
        raise Skip("Manual runs must use the default branch")
    base = api(f"repos/{repo}/commits/{urllib.parse.quote(branch, safe='')}")["sha"]
    require(r"[a-f0-9]{40}", base, "base commit")
    contents = api(f"repos/{repo}/contents/factory.toml?ref={base}")
    if contents.get("encoding") != "base64" or contents.get("type") != "file":
        raise ValueError("factory.toml must be a regular file")
    config = config_from_toml(base64.b64decode(contents["content"]).decode(), agent)
    task = {"repository": repo, "issue": issue_number, "agent": agent,
            "title": issue["title"], "body": issue.get("body") or "", "comment": comment,
            "base_sha": base, "base_branch": branch, "config": config,
            "run_id": env["GITHUB_RUN_ID"], "branch": f"jio/issue-{issue_number}-{env['GITHUB_RUN_ID']}"}
    validate_task(task)
    if len(json.dumps(task).encode()) > 64 * 1024:
        raise ValueError("Task context exceeds 64 KiB; shorten the issue")
    return task


def preflight_account(client, key):
    # The Python SDK has no usage binding yet; read the existing hosted route.
    context = ssl.create_default_context(cafile=os.environ["JIO_CA_CERT"])
    usage = request_json(client.endpoint.rstrip("/") + "/v1/usage", key, context=context)
    if usage.get("session_ttl_seconds") != 3600:
        raise ValueError("Use a dedicated factory account with a 3600-second session expiry")
    limits = usage.get("limits", {})
    if limits.get("cpu") != 2 or limits.get("memory_mib") != 4096 or limits.get("disk_mib", 0) < 65792:
        raise ValueError("Factory account requires 2 vCPU, 4096 MiB RAM and at least 65792 MiB disk")
    if usage.get("reserved") != {"cpu": 0, "memory_mib": 0, "disk_mib": 0}:
        raise ValueError("Factory account is occupied; resolve its existing reservation before retrying")


def scrub(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    # Remove terminal control codes and neutralize GitHub workflow commands.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return text.replace("\r", "").replace("::", ": :")


def credential_secrets(agent, credential):
    values = [credential]
    if agent == "codex" and credential.lstrip().startswith("{"):
        auth = json.loads(credential)
        if auth.get("auth_mode") != "chatgpt" or not isinstance(auth.get("tokens"), dict):
            raise ValueError("CODEX_AUTH_JSON must contain a Codex ChatGPT login")
        values.extend(value for value in auth["tokens"].values() if isinstance(value, str) and value)
    return values


def agent_result(agent, output):
    if agent == "claude":
        result = json.loads(output)
        if result.get("is_error") or result.get("subtype") != "success":
            raise RuntimeError("Claude did not complete the task successfully")
        return str(result.get("result", ""))[:16000]
    events = [json.loads(line) for line in output.splitlines() if line.strip()]
    if not events or events[-1].get("type") != "turn.completed":
        raise RuntimeError("Codex did not complete the task successfully")
    messages = [event["item"].get("text", "") for event in events
                if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message"]
    return (messages[-1] if messages else "Agent completed.")[:16000]


def cleanup(client, state_file):
    if not state_file.exists():
        return
    state = read_json(state_file)
    if state.get("vm") and not state.get("destroyed"):
        vm = require(r"[a-f0-9]{32}", state["vm"], "VM ID")
        client.destroy(vm)
        state["destroyed"] = True
        write_json(state_file, state)


def work(task, client, output, state_file, provider_key, jio_key, preflight=preflight_account):
    validate_task(task)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    outcome = {"passed": False, "stage": "preflight", "cleanup": "not needed"}
    secrets = [provider_key, jio_key]
    started = time.monotonic()
    log = []

    def run(operation, payload, timeout=120):
        outcome["stage"] = operation
        print(f"Jio task: {operation}", flush=True)
        remaining = int(40 * 60 - (time.monotonic() - started))
        if remaining < 1:
            raise TimeoutError("Task deadline reached")
        result = vm.exec(f"python3 {GUEST} {operation}", input=json.dumps(payload).encode(),
                         timeout=min(timeout, remaining))
        if operation not in {"agent", "patch"}:
            log.append(f"[{operation}]\n{result.stdout_text}\n{result.stderr_text}")
        if not result.success:
            # Never publish raw agent transcripts or their tool output.
            raise RuntimeError(f"{operation} exited with status {result.exit_code}")
        return result.stdout

    try:
        if not provider_key or not jio_key:
            raise ValueError("Add JIO_API_KEY and the selected provider API key before running")
        secrets = credential_secrets(task["agent"], provider_key) + [jio_key]
        preflight(client, jio_key)
        write_json(state_file, {"create_attempted": True})
        outcome["stage"] = "create"
        outcome["cleanup"] = "creation outcome unknown; account expiry is the fallback"
        vm = client.create()  # Hosted v0 endpoint defaults to Medium.
        write_json(state_file, {"vm": vm.id, "destroyed": False})
        outcome["vm"] = vm.id
        outcome["cleanup"] = "pending"
        vm.exec("install -d -m 700 /home/jio/.software-factory", timeout=30).raise_for_status()
        vm.write_file(GUEST, SOURCE.joinpath("guest.py").read_bytes(), timeout=30)
        run("clone", task)
        run("setup", {"command": task["config"]["setup"]}, 600)
        run("install", {"agent": task["agent"]}, 300)
        prompt = ("Work on the assigned GitHub issue in this repository. Read and follow its "
                  "AGENTS.md, GOALS.md and CLAUDE.md instructions where present. Implement a focused "
                  "change, add appropriate tests, and run the project checks. Do not push, create a "
                  "PR, change factory.toml or .github files, or include credentials. Leave changes "
                  "in the working tree. Summarize the change, tests and limitations.\n\n"
                  + json.dumps({key: task[key] for key in ("repository", "issue", "title", "body", "comment")})
                  + "\nProject checks:\n" + task["config"]["test"])
        raw = run("agent", {"agent": task["agent"], "model": task["config"]["models"][task["agent"]],
                            "key": provider_key, "prompt": prompt}, 1800)
        outcome["summary"] = scrub(agent_result(task["agent"], raw.decode()), secrets)
        patch = run("patch", task)
        if not patch or len(patch) > MAX_PATCH:
            raise ValueError("Agent must produce a nonempty patch of at most 4 MiB")
        if any(secret.encode() in patch for secret in secrets):
            raise ValueError("Credential detected in patch; refusing publication")
        run("test", {"command": task["config"]["test"]}, 600)
        if patch != run("patch", task):
            raise ValueError("Tests changed the candidate; refusing to publish different code")
        output.joinpath("change.patch").write_bytes(patch)
        outcome.update(passed=True, patch_sha256=hashlib.sha256(patch).hexdigest(), stage="complete")
    except Exception as error:  # noqa: BLE001 -- Every SDK failure must leave evidence and trigger cleanup.
        outcome["error"] = scrub(str(error), secrets)
    finally:
        try:
            cleanup(client, state_file)
            if outcome.get("vm"):
                outcome["cleanup"] = "destroyed"
        except Exception as error:  # noqa: BLE001 -- Preserve the task result if cleanup itself fails.
            outcome["cleanup"] = "failed; account expiry is the fallback"
            outcome["cleanup_error"] = scrub(str(error), secrets)
        outcome["elapsed_seconds"] = round(time.monotonic() - started, 1)
        write_json(output / "outcome.json", outcome)
        output.joinpath("tests.txt").write_text(scrub("\n".join(log), secrets)[-128000:])
    return outcome


def git(command, directory, env):
    # Never run hooks, filters, credential helpers, or code from the candidate.
    args = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *command]
    with tempfile.TemporaryFile() as capture:
        result = subprocess.run(args, cwd=directory, env=env, stdout=capture, stderr=capture, timeout=120, check=False)
        capture.seek(0)
        data = capture.read(MAX_JSON + 1)
    if result.returncode:
        raise RuntimeError(f"git {command[0]} failed (status {result.returncode})")
    if len(data) > MAX_JSON:
        raise ValueError("Git output exceeds 2 MiB")
    return data


def git_environment(home):
    return {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Jio Factory", "GIT_COMMITTER_NAME": "Jio Factory",
            "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com"}


def apply_patch(patch, directory, env):
    git(["apply", "--check", "--index", "--binary", str(patch)], directory, env)
    git(["apply", "--index", "--binary", str(patch)], directory, env)
    paths = git(["diff", "--cached", "--name-only", "-z"], directory, env).split(b"\0")
    for path in filter(None, paths):
        parts = path.decode().split("/")
        if any(part in {".git", ".github", ".gitattributes", ".gitmodules"} for part in parts) or path == b"factory.toml":
            raise ValueError("Factory tasks cannot change workflow/configuration control files")
    if not any(paths):
        raise ValueError("Empty patch")


def publish(task, output, api=github):
    validate_task(task)
    outcome = read_json(output / "outcome.json")
    if not outcome.get("passed") or outcome.get("cleanup") != "destroyed":
        raise ValueError("Tests and VM cleanup must pass before publishing")
    patch = output / "change.patch"
    if not 0 < patch.stat().st_size <= MAX_PATCH or hashlib.sha256(patch.read_bytes()).hexdigest() != outcome["patch_sha256"]:
        raise ValueError("Patch does not match tested artifact")
    previous = existing_pr(task, api)
    if previous:
        return previous["html_url"]
    with tempfile.TemporaryDirectory(prefix="factory-publish-") as directory:
        env = git_environment(directory)
        git(["init", "-q"], directory, env)
        git(["fetch", "--depth=1", f"https://github.com/{task['repository']}.git", task["base_sha"]], directory, env)
        git(["checkout", "--detach", "FETCH_HEAD"], directory, env)
        apply_patch(patch.resolve(), directory, env)
        timestamp = git(["show", "-s", "--format=%cI", "HEAD"], directory, env).decode().strip()
        env.update(GIT_AUTHOR_DATE=timestamp, GIT_COMMITTER_DATE=timestamp)
        git(["commit", "--no-gpg-sign", "-m", f"feat: address issue #{task['issue']}"], directory, env)
        candidate = git(["rev-parse", "HEAD"], directory, env).decode().strip()
        # Credentials go in the subprocess environment, never its argv or local Git config.
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + base64.b64encode(
                       ("x-access-token:" + os.environ["GH_TOKEN"]).encode()).decode())
        try:
            remote = api(f"repos/{task['repository']}/git/ref/heads/{task['branch']}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            remote = None
        if remote is None:
            # Empty lease means create ONLY: never overwrite an existing branch, even in a race.
            git(["push", f"--force-with-lease=refs/heads/{task['branch']}:",
                 f"https://github.com/{task['repository']}.git", f"HEAD:refs/heads/{task['branch']}"], directory, env)
        elif remote.get("object", {}).get("sha") != candidate:
            raise RuntimeError("Factory branch already has different code; inspect it before retrying")
    body = (f"Addresses #{task['issue']}.\n\n{outcome.get('summary', 'Task completed.')}\n\n"
            f"Validation: `{task['config']['test']}` passed in Jio. VM destroyed.\n\n"
            f"Base: `{task['base_sha']}` · Agent: `{task['agent']}`\n\n"
            f"[Workflow and evidence](https://github.com/{task['repository']}/actions/runs/{task['run_id']})\n\n"
            "Draft for human review. Agent output and tests do not replace code review.")
    pr = api(f"repos/{task['repository']}/pulls", "POST",
             {"title": f"{task['title']}"[:240], "body": body, "head": task["branch"],
              "base": task["base_branch"], "draft": True})
    return pr["html_url"]


def report(task, output, status, url=None):
    link = f"https://github.com/{task['repository']}/actions/runs/{task['run_id']}"
    message = f"Jio factory ({task['agent']}): **{status}**. [Run and evidence]({link})."
    if url:
        message += f"\n\nDraft PR: {url}"
    if output.joinpath("outcome.json").exists():
        outcome = read_json(output / "outcome.json")
        message += f"\n\nVM cleanup: {outcome.get('cleanup', 'unknown')}."
        if outcome.get("error"):
            message += "\n\nSee the run artifacts for the failing stage and diagnostic output."
    github(f"repos/{task['repository']}/issues/{task['issue']}/comments", "POST", {"body": message})
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(message + "\n")


def main():
    os.umask(0o077)
    output = Path(os.environ.get("FACTORY_OUTPUT_DIR", "out")).resolve()
    state_file = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "factory-vm.json"
    command = sys.argv[1] if len(sys.argv) == 2 else ""
    if command == "authorize":
        try:
            task = authorize(read_json(Path(os.environ["GITHUB_EVENT_PATH"])), os.environ)
            if existing_pr(task):
                raise Skip("This workflow run already produced a PR")
        except Skip as skipped:
            print(f"Ignored: {skipped}")
            return
        write_json(output / "task.json", task)
        with open(os.environ["GITHUB_OUTPUT"], "a") as file:
            file.write(f"eligible=true\nagent={task['agent']}\n")
    elif command in {"work", "cleanup"}:
        from jio import Jio
        client = Jio(api_key=os.environ["JIO_API_KEY"], state_dir=os.environ["JIO_STATE_DIR"])
        if command == "cleanup":
            cleanup(client, state_file)
            return
        def interrupted(signum, frame):
            raise InterruptedError("Workflow interrupted")
        signal.signal(signal.SIGTERM, interrupted)
        task = read_json(output / "task.json")
        outcome = work(task, client, output, state_file,
                       os.environ.get("PROVIDER_API_KEY", ""), os.environ["JIO_API_KEY"])
        if not outcome["passed"] or outcome["cleanup"] != "destroyed":
            raise RuntimeError("Task failed; inspect outcome.json and tests.txt")
    elif command == "publish":
        task = read_json(output / "task.json")
        status, url = "failed", None
        try:
            if os.environ.get("WORK_RESULT") != "success":
                raise RuntimeError("VM job did not succeed; no PR will be published")
            url = publish(task, output)
            status = "completed"
        finally:
            report(task, output, status, url)
    else:
        raise SystemExit("usage: factory.py authorize|work|cleanup|publish")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- Redact diagnostics at the process boundary.
        # Keep HTTP bodies, SDK diagnostics and credentials out of Actions logs.
        message = scrub(str(error), [os.environ.get(key, "") for key in
                                    ("GH_TOKEN", "JIO_API_KEY", "PROVIDER_API_KEY")])
        print(f"Factory stopped: {message}", file=sys.stderr)
        raise SystemExit(1)
