"""Guest operations. Receives credentials on stdin only for the agent invocation."""
import json
import os
import subprocess
import sys
from pathlib import Path

CODEX_VERSION = "0.154.0"
CLAUDE_VERSION = "2.1.81"
ROOT = Path.home() / "software-factory-task"
CONTROL = Path.home() / ".software-factory"


def agent_command(agent, model):
    if agent == "codex":
        # The disposable Jio VM is the sandbox; its rootfs cannot run nested bwrap.
        return [
            "codex", "exec", "--json", "--ephemeral", "--ignore-user-config",
            "--ignore-rules", "--sandbox", "danger-full-access", "--color", "never",
            "-c", 'approval_policy="never"',
            "-c", 'shell_environment_policy.exclude=["CODEX_API_KEY"]',
            "--model", model, "-",
        ]
    if agent == "claude":
        return [
            "claude", "--bare", "--print", "--output-format", "json",
            "--no-session-persistence", "--no-chrome", "--disable-slash-commands",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "", "--permission-mode", "dontAsk",
            "--tools", "Read,Edit,Write,Glob,Grep,Bash",
            "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash", "--model", model,
        ]
    raise ValueError("Choose codex or claude")


def main():
    os.umask(0o077)
    data = json.load(sys.stdin)
    operation = sys.argv[1]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("JIO_", "GH_", "GITHUB_", "CODEX_", "ANTHROPIC_", "OPENAI_"))}
    env.update(CI="true", GIT_TERMINAL_PROMPT="0", npm_config_update_notifier="false")
    if operation == "clone":
        ROOT.mkdir(mode=0o700)
        subprocess.run(["git", "init", "-q", str(ROOT)], check=True, env=env)
        subprocess.run(["git", "-C", str(ROOT), "fetch", "--depth=1",
                        f"https://github.com/{data['repository']}.git", data["base_sha"]],
                       check=True, env=env)
        subprocess.run(["git", "-C", str(ROOT), "checkout", "--detach", "FETCH_HEAD"],
                       check=True, env=env)
        return
    env["PATH"] = str(CONTROL / "bin") + os.pathsep + env["PATH"]
    if operation == "install":
        package = (f"@openai/codex@{CODEX_VERSION}" if data["agent"] == "codex"
                   else f"@anthropic-ai/claude-code@{CLAUDE_VERSION}")
        subprocess.run(["npm", "install", "--global", "--prefix", str(CONTROL),
                        "--no-audit", "--no-fund", package], check=True, env=env)
        expected = CODEX_VERSION if data["agent"] == "codex" else CLAUDE_VERSION
        actual = subprocess.check_output([data["agent"], "--version"], env=env, text=True)
        if expected not in actual.split():
            raise RuntimeError("Installed agent version does not match the pin")
    elif operation in {"setup", "test"}:
        subprocess.run(["bash", "-e", "-o", "pipefail", "-c", data["command"]],
                       cwd=ROOT, env=env, check=True)
    elif operation == "agent":
        env["CODEX_HOME"] = str(CONTROL / "codex")
        if data["agent"] == "codex" and data["key"].lstrip().startswith("{"):
            auth = json.loads(data["key"])
            if auth.get("auth_mode") != "chatgpt" or not auth.get("tokens", {}).get("access_token"):
                raise ValueError("CODEX_AUTH_JSON must contain a Codex ChatGPT login")
            home = Path(env["CODEX_HOME"])
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
            auth_file = home / "auth.json"
            auth_file.write_text(json.dumps(auth))
            auth_file.chmod(0o600)
        else:
            env["CODEX_API_KEY" if data["agent"] == "codex" else "ANTHROPIC_API_KEY"] = data["key"]
        result = subprocess.run(agent_command(data["agent"], data["model"]),
                                input=data["prompt"].encode(), cwd=ROOT, env=env, check=False)
        raise SystemExit(result.returncode)
    elif operation == "patch":
        # Include new files and agent-made commits, not just changes against HEAD.
        git = ["git", "-C", str(ROOT), "-c", "core.hooksPath=/dev/null"]
        subprocess.run([*git, "add", "--all"], check=True, env=env)
        subprocess.run([*git, "diff", "--cached", "--binary", "--no-ext-diff",
                        "--no-textconv", data["base_sha"], "--"], check=True, env=env)
    else:
        raise ValueError("Unknown guest operation")


if __name__ == "__main__":
    main()
