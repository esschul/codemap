"""
Batch Git stats for heatmap overlays.

Runs two git commands against all component files at once:
  1. git log --format=... --name-only  → last-changed timestamp per file
  2. git log --since=1.year --numstat  → commit count + lines changed per file (12m)

Returns a dict keyed by absolute file path string.
"""
from __future__ import annotations
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitFileStats:
    last_changed_ts: int | None = None   # unix timestamp of most recent commit
    commits_12m: int = 0                 # number of commits touching this file in last 12 months
    lines_12m: int = 0                   # added + deleted lines in last 12 months


def collect_git_stats(files: list[Path], root: Path) -> dict[str, GitFileStats]:
    """Returns {abs_path_str: GitFileStats} for each file that has git history."""
    if not files:
        return {}

    # Find the git repo root (may differ from the Spring source root)
    try:
        git_root_out = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=root.resolve(), capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
        abs_root = Path(git_root_out)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return {}

    # Always work with absolute paths so we can match git output regardless of cwd
    _ = root.resolve()  # unused but keeps variable name consistent
    abs_files = [f.resolve() for f in files]
    file_strs = [str(f) for f in abs_files]
    result: dict[str, GitFileStats] = {s: GitFileStats() for s in file_strs}

    # ── Pass 1: recency ───────────────────────────────────────────────────────
    # git log --format="COMMIT %ct" --name-only -- file1 file2 ...
    # Output interleaves: "COMMIT <ts>" lines then filenames then blank lines.
    # We track the current timestamp and assign it to every following filename.
    try:
        out = subprocess.run(
            ['git', 'log', '--format=COMMIT %ct', '--name-only', '--'] + file_strs,
            cwd=abs_root, capture_output=True, text=True, timeout=60
        ).stdout
        current_ts: int | None = None
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith('COMMIT '):
                current_ts = int(line[7:])
            elif current_ts is not None:
                # line is a relative path from repo root
                abs_path = str((abs_root / line).resolve())
                if abs_path in result and result[abs_path].last_changed_ts is None:
                    result[abs_path].last_changed_ts = current_ts
    except Exception:
        pass

    # ── Pass 2: churn (last 12 months) ───────────────────────────────────────
    # git log --since=1.year --numstat --format=%H -- file1 file2 ...
    # Output: commit-hash lines then blank line then "<added>\t<deleted>\t<path>" lines.
    # We count one commit per commit-hash line and accumulate lines per file.
    try:
        out = subprocess.run(
            ['git', 'log', '--since=1.year', '--numstat', '--format=%H', '--'] + file_strs,
            cwd=abs_root, capture_output=True, text=True, timeout=60
        ).stdout
        # Track which files we've already counted for this commit (avoid double-counting)
        counted_in_commit: set[str] = set()
        for line in out.splitlines():
            line = line.strip()
            if not line:
                counted_in_commit.clear()
                continue
            if '\t' not in line:
                # This is a commit hash line — new commit
                counted_in_commit.clear()
                continue
            parts = line.split('\t')
            if len(parts) >= 3:
                added_str, deleted_str, rel_path = parts[0], parts[1], parts[2]
                abs_path = str((abs_root / rel_path).resolve())
                if abs_path in result and abs_path not in counted_in_commit:
                    try:
                        added = int(added_str) if added_str != '-' else 0
                        deleted = int(deleted_str) if deleted_str != '-' else 0
                        result[abs_path].commits_12m += 1
                        result[abs_path].lines_12m += added + deleted
                        counted_in_commit.add(abs_path)
                    except ValueError:
                        pass
    except Exception:
        pass

    return result
