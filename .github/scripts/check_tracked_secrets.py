"""Fail CI on common credential shapes or sensitive local-state filenames.

This is a narrow accidental-disclosure gate, not a comprehensive secret scan.
It only reports paths and pattern labels; never print matched credential bytes.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTENT_PATTERNS = {
    "api_key": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(rb"gh[pousr]_[A-Za-z0-9_]{30,}"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".db"}


def path_issue(path: Path) -> str | None:
    if path.name == ".env" or path.name.startswith(".env."):
        return "environment_file"
    if path.suffix.lower() in SENSITIVE_SUFFIXES or path.name in {"id_rsa", "id_ed25519"}:
        return "sensitive_state_or_key_file"
    return None


def content_issues(contents: bytes) -> list[str]:
    return [label for label, expression in CONTENT_PATTERNS.items()
            if expression.search(contents)]


def main() -> None:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                            stdout=subprocess.PIPE, check=True).stdout
    findings = []
    for raw in listed.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8", errors="surrogateescape"))
        path = ROOT / relative
        if not path.is_file():
            continue
        issue = path_issue(relative)
        if issue:
            findings.append((relative.as_posix(), issue))
        for label in content_issues(path.read_bytes()):
            findings.append((relative.as_posix(), label))
    if findings:
        for name, label in findings:
            print(f"Potential tracked secret: {name} ({label})")
        raise SystemExit(f"Tracked secret gate failed: {len(findings)} finding(s)")
    print("Tracked secret gate passed (common patterns and sensitive filenames)")


if __name__ == "__main__":
    main()
