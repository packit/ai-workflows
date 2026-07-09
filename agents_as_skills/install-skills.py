#!/usr/bin/env python3
"""Install Ymir agent skills and MCP tools for local use with Cursor/Claude Code.

This script:
1. Prompts for required credentials (tokens, email, etc.)
2. Downloads SKILL.md files from the upstream repo into the client's skills directory
3. Creates a Python 3.13 venv and installs ymir-common + ymir-tools
4. Writes MCP server configuration directly to the client's config file

Usage:
    python3 scripts/install-skills.py
"""

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://raw.githubusercontent.com/packit/ai-workflows/main/agents_as_skills"
SKILLS = [
    "backport",
    "rebase",
    "triage",
    "rebuild",
    "preliminary-testing",
    "issue-verification",
    "errata-workflow",
]

VENV_PATH = Path.home() / ".local" / "share" / "ymir-venv"

CLIENT_SKILLS_DIRS = {
    "cursor": Path.home() / ".cursor" / "skills",
    "claude": Path.home() / ".claude" / "skills",
    "opencode": Path.home() / ".config" / "opencode" / "skills",
}

CLIENT_MCP_CONFIG_PATHS = {
    "cursor": Path.home() / ".cursor" / "mcp.json",
    "claude": Path.home() / ".claude.json",
    "opencode": Path.home() / ".config" / "opencode" / "opencode.json",
}


def prompt_value(label: str, env_var: str = None, default: str = None, secret: bool = False) -> str:
    existing = os.environ.get(env_var) if env_var else None
    if existing:
        masked = existing[:4] + "..." if secret and len(existing) > 4 else existing
        use_existing = input(f"  {label}: found env ${env_var} = {masked}. Use it? [Y/n]: ").strip().lower()
        if use_existing != "n":
            return existing

    prompt_text = f"  {label}"
    if default:
        prompt_text += f" [{default}]"
    prompt_text += ": "

    if secret:
        value = getpass.getpass(prompt_text)
    else:
        value = input(prompt_text)

    return value.strip() or default or ""


def prompt_choice(label: str, choices: list[str], default: str = None) -> str:
    print(f"\n  {label}")
    for i, choice in enumerate(choices, 1):
        marker = " (default)" if choice == default else ""
        print(f"    {i}. {choice}{marker}")

    while True:
        raw = input(f"  Choose [1-{len(choices)}]: ").strip()
        if not raw and default:
            return default
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        except ValueError:
            if raw in choices:
                return raw
        print(f"  Please enter a number between 1 and {len(choices)}")


def run(cmd, **kwargs):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"  ERROR: command failed with exit code {result.returncode}")
        sys.exit(1)
    return result


def collect_credentials() -> dict:
    print("\n" + "=" * 60)
    print("  Credentials Setup")
    print("  (values are read from env vars if set, otherwise prompted)")
    print("=" * 60)

    creds = {}

    creds["JIRA_URL"] = prompt_value(
        "Jira URL", env_var="JIRA_URL",
        default="https://redhat.atlassian.net"
    )

    creds["JIRA_EMAIL"] = prompt_value(
        "Jira email", env_var="JIRA_EMAIL"
    )

    creds["JIRA_TOKEN"] = prompt_value(
        "Jira API token", env_var="JIRA_TOKEN", secret=True
    )

    creds["GITLAB_TOKEN"] = prompt_value(
        "GitLab personal access token", env_var="GITLAB_TOKEN", secret=True
    )

    print("\n  Kerberos configuration:")
    klist_result = subprocess.run(["klist"], capture_output=True)
    if klist_result.returncode == 0:
        print("  Kerberos ticket found (klist succeeded).")
    else:
        print("  WARNING: No active Kerberos ticket. Run 'kinit' before using the tools.")

    use_keytab = input("  Do you have a keytab file for automated kinit? [y/N]: ").strip().lower() == "y"
    if use_keytab:
        creds["KEYTAB_FILE"] = prompt_value(
            "Keytab file path", env_var="KEYTAB_FILE",
            default=str(Path.home() / ".secrets" / "keytab")
        )

    krb5cc = os.environ.get("KRB5CCNAME")
    if krb5cc:
        creds["KRB5CCNAME"] = krb5cc
        print(f"  Using KRB5CCNAME from env: {krb5cc}")

    return creds


def install_skills(client: str):
    skills_dir = CLIENT_SKILLS_DIRS[client]

    print(f"\n=> Installing skills to {skills_dir}")
    for skill in SKILLS:
        skill_dir = skills_dir / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        url = f"{REPO_URL}/{skill}/SKILL.md"
        dest = skill_dir / "SKILL.md"
        print(f"  Downloading {skill}...")
        run(["curl", "-fsSL", url, "-o", str(dest)])

    print(f"\n=> {len(SKILLS)} skills installed to {skills_dir}")


def install_venv():
    print(f"\n=> Creating virtual environment at {VENV_PATH}")

    python = "python3.13"
    result = subprocess.run([python, "--version"], capture_output=True)
    if result.returncode != 0:
        python = "python3"
        print(f"  python3.13 not found, falling back to {python}")
        print("  WARNING: BeeAI framework requires Python < 3.14. Ensure your Python is compatible.")

    if not VENV_PATH.exists():
        run([python, "-m", "venv", str(VENV_PATH)])
    else:
        print(f"  Venv already exists at {VENV_PATH}")

    pip = str(VENV_PATH / "bin" / "pip")
    print("\n=> Installing ymir-common...")
    run([pip, "install", "--upgrade",
         "git+https://github.com/packit/ai-workflows.git#subdirectory=ymir/common"])

    print("\n=> Installing ymir-tools...")
    run([pip, "install", "--upgrade",
         "git+https://github.com/packit/ai-workflows.git#subdirectory=ymir/tools"])

    priv_gw = VENV_PATH / "bin" / "ymir-privileged-gateway"
    unpriv_gw = VENV_PATH / "bin" / "ymir-unprivileged-gateway"
    if priv_gw.exists() and unpriv_gw.exists():
        print(f"\n=> Gateways installed:")
        print(f"   Privileged:   {priv_gw}")
        print(f"   Unprivileged: {unpriv_gw}")
    else:
        print("\n  WARNING: Gateway entry points not found. Check installation logs.")


def write_mcp_config(client: str, creds: dict):
    priv_gw = str(VENV_PATH / "bin" / "ymir-privileged-gateway")
    unpriv_gw = str(VENV_PATH / "bin" / "ymir-unprivileged-gateway")

    priv_env = {"MCP_TRANSPORT": "stdio"}
    for key in ("GITLAB_TOKEN", "JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN", "KRB5CCNAME", "KEYTAB_FILE"):
        if key in creds and creds[key]:
            priv_env[key] = creds[key]

    unpriv_env = {
        "MCP_TRANSPORT": "stdio",
        "UPSTREAM_SEARCH_API_URL": "http://upstream-search.hosted.upshift.rdu2.redhat.com:80/v1",
        "REQUESTS_CA_BUNDLE": "/etc/pki/tls/certs/ca-bundle.crt",
    }

    config_path = CLIENT_MCP_CONFIG_PATHS[client]

    if client == "opencode":
        new_servers = {
            "ymir-privileged": {
                "type": "local",
                "command": [priv_gw],
                "env": priv_env,
            },
            "ymir-unprivileged": {
                "type": "local",
                "command": [unpriv_gw],
                "env": unpriv_env,
            },
        }
        config_key = "mcp"
    else:
        new_servers = {
            "ymir-privileged": {
                "command": priv_gw,
                "env": priv_env,
            },
            "ymir-unprivileged": {
                "command": unpriv_gw,
                "env": unpriv_env,
            },
        }
        config_key = "mcpServers"

    existing_config = {}
    if config_path.exists():
        try:
            existing_config = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if config_key not in existing_config:
        existing_config[config_key] = {}
    existing_config[config_key].update(new_servers)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(existing_config, indent=2) + "\n")
    print(f"\n=> MCP config written to {config_path}")


def main():
    print("=" * 60)
    print("  Ymir Skills Installer")
    print("=" * 60)

    client = prompt_choice(
        "Which agent client do you use?",
        choices=["cursor", "claude", "opencode"],
        default="cursor",
    )

    creds = collect_credentials()

    print("\n" + "=" * 60)
    print("  Installing...")
    print("=" * 60)

    install_skills(client)
    install_venv()
    write_mcp_config(client, creds)

    print("\n" + "=" * 60)
    print("  Installation complete!")
    print("=" * 60)
    print(f"\n  Skills:     {CLIENT_SKILLS_DIRS[client]}")
    print(f"  Venv:       {VENV_PATH}")
    print(f"  MCP config: {CLIENT_MCP_CONFIG_PATHS[client]}")
    print(f"\n  Next steps:")
    if "KEYTAB_FILE" not in creds:
        print(f"  1. Run 'kinit <your-kerberos-id>@IPA.REDHAT.COM'")
    print(f"  2. Restart your {client} client")
    print(f"  3. Verify MCP servers are connected (in Cursor: check MCP panel)")
    print(f"  4. Try: 'Use the triage skill with jira_issue=RHEL-12345'")


if __name__ == "__main__":
    main()
