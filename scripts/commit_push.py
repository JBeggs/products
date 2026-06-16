#!/usr/bin/env python3
"""
Run tests with coverage, then add, commit, and push changes.
On test failure, stops and exits. Uses SSH for Git.
"""
import os
import subprocess
import sys
from pathlib import Path

# Resolve project root (django-crm) and scripts dir
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent


def project_python() -> str:
    """Prefer .venv Python so coverage/pytest match project deps."""
    for name in ("python", "python3"):
        candidate = PROJECT_ROOT / ".venv" / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def load_env():
    """Load .env from project root."""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            pass


def get_git_env():
    """Return env dict for git subprocess, with SSH_ASKPASS if passphrase set."""
    env = dict(os.environ)
    passphrase = os.environ.get("SSH_KEY_PASSPHRASE", "").strip()
    if passphrase:
        askpass = SCRIPT_DIR / "ssh_askpass.py"
        if askpass.exists():
            env["SSH_ASKPASS"] = str(askpass)
            env["SSH_ASKPASS_REQUIRE"] = "force"
    return env


def main():
    load_env()
    python = project_python()
    if python != sys.executable:
        print(f"Using project venv: {python}")
    else:
        print(f"Using interpreter: {python} (.venv not found)")

    print("Running tests with coverage...")
    result = subprocess.run(
        [python, "-m", "coverage", "run", "-m", "pytest", "-v"],
        cwd=PROJECT_ROOT,
        capture_output=False,
    )
    if result.returncode != 0:
        print("\nTests failed. Fix the failures and run this script again.")
        sys.exit(1)

    print("\nGenerating coverage report...")
    subprocess.run(
        [python, "-m", "coverage", "report", "--show-missing"],
        cwd=PROJECT_ROOT,
        check=True,
    )

    subprocess.run([python, "-m", "coverage", "html"], cwd=PROJECT_ROOT, check=True)

    print("\nAll tests passed.")
    print("Coverage report: htmlcov/index.html")

    print("\nStaging changes...")
    subprocess.run(["git", "add", "-A"], cwd=PROJECT_ROOT, check=True)

    commit_msg = input("Enter commit message: ").strip()
    if not commit_msg:
        print("Commit message required.")
        sys.exit(1)

    print("Committing...")
    result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").lower()
        if "nothing to commit" in err or "no changes" in err:
            print("Nothing to commit (working tree clean).")
            sys.exit(0)
        print(result.stderr or result.stdout or "Commit failed.")
        sys.exit(1)

    print("Pushing to remote...")
    subprocess.run(
        ["git", "push"],
        cwd=PROJECT_ROOT,
        env=get_git_env(),
        check=True,
    )

    print("\nCommit and push complete.")


if __name__ == "__main__":
    main()
