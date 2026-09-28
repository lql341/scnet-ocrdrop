#!/usr/bin/env python3
"""Fail when public repository content contains credentials or personal paths."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_BYTES = 2 * 1024 * 1024
PLACEHOLDER_USERS = {
    "EXAMPLE",
    "EXAMPLE_USER",
    "OTHER_USER",
    "USER",
    "USERNAME",
}
PATTERNS: Sequence[Tuple[str, re.Pattern[str]]] = (
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "GitHub token",
        re.compile(
            r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})\b"
        ),
    ),
    (
        "private key",
        re.compile(
            "-----BEGIN " + r"(?:RSA |EC |OPENSSH )?" + "PRIVATE KEY-----"
        ),
    ),
    (
        "credential-bearing URL",
        re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
    ),
    (
        "macOS user path",
        re.compile(r"/Users/(?P<user>[A-Za-z0-9._-]+)(?:/|$)"),
    ),
    (
        "Linux user path",
        re.compile(r"/(?:public/)?home/(?P<user>[A-Za-z0-9._-]+)(?:/|$)"),
    ),
    (
        "Windows user path",
        re.compile(r"[A-Za-z]:\\Users\\(?P<user>[A-Za-z0-9._-]+)(?:\\|$)"),
    ),
)
COARSE_HISTORY_PATTERN = (
    r"(/Users/|/public/home/|/home/|[A-Za-z]:\\Users\\|"
    r"AKIA[0-9A-Z]{16}|github_pat_|gh[pousr]_|"
    r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|https?://[^[:space:]]+@)"
)


def command(args: Sequence[str], *, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args),
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=False,
    )


def tracked_and_untracked_paths() -> List[Path]:
    result = command(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        text=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return [
        ROOT / raw.decode("utf-8", "surrogateescape")
        for raw in result.stdout.split(b"\0")
        if raw
    ]


def read_text(path: Path) -> Optional[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_TEXT_BYTES or b"\0" in data:
        return None
    return data.decode("utf-8", "replace")


def findings(text: str) -> Iterable[Tuple[int, str]]:
    for line_number, line in enumerate(text.splitlines(), 1):
        for label, pattern in PATTERNS:
            for match in pattern.finditer(line):
                user = match.groupdict().get("user")
                if user and user.upper() in PLACEHOLDER_USERS:
                    continue
                yield line_number, label


def scan_worktree() -> List[str]:
    result = []
    for path in tracked_and_untracked_paths():
        text = read_text(path)
        if text is None:
            continue
        relative = path.relative_to(ROOT)
        for line_number, label in findings(text):
            result.append("%s:%d: %s" % (relative, line_number, label))
    return result


def history_candidate_paths(commit: str) -> List[str]:
    result = command(
        [
            "git",
            "grep",
            "-l",
            "-I",
            "-E",
            COARSE_HISTORY_PATTERN,
            commit,
            "--",
            ".",
        ]
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip())
    paths = []
    for line in result.stdout.splitlines():
        prefix = commit + ":"
        paths.append(line[len(prefix) :] if line.startswith(prefix) else line)
    return paths


def scan_history() -> List[str]:
    commits = command(["git", "rev-list", "--all"])
    if commits.returncode != 0:
        raise RuntimeError(commits.stderr.strip())
    result = []
    seen = set()
    for commit in commits.stdout.splitlines():
        for path in history_candidate_paths(commit):
            shown = command(["git", "show", "%s:%s" % (commit, path)], text=False)
            if shown.returncode != 0 or b"\0" in shown.stdout:
                continue
            text = shown.stdout.decode("utf-8", "replace")
            for line_number, label in findings(text):
                marker = (commit, path, line_number, label)
                if marker in seen:
                    continue
                seen.add(marker)
                result.append(
                    "%s:%s:%d: %s"
                    % (commit[:12], path, line_number, label)
                )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--history",
        action="store_true",
        help="also scan every commit reachable from local refs",
    )
    args = parser.parse_args()
    problems = scan_worktree()
    if args.history:
        problems.extend(scan_history())
    if problems:
        print("public release audit failed:", file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1
    scope = "working tree and history" if args.history else "working tree"
    print("public release audit passed (%s)" % scope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
