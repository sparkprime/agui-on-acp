"""Git operations REST API routes."""

import asyncio
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from backend.types.api import (
    GitCommitRequest,
    GitCommitResponse,
    GitDiffResponse,
    GitLogEntry,
    GitLogResponse,
    GitStatus,
    GitStatusFile,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _run_git(args: list[str], cwd: str) -> tuple[str, str, int]:
    """Run a git command and return stdout, stderr, and return code.

    Args:
        args: Git command arguments
        cwd: Working directory

    Returns:
        Tuple of (stdout, stderr, returncode)
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        proc.returncode or 0,
    )


def _parse_status_line(line: str) -> GitStatusFile | None:
    """Parse a git status --porcelain line.

    Args:
        line: Status line (e.g., " M file.txt" or "?? new.txt")

    Returns:
        GitStatusFile or None if invalid
    """
    if len(line) < 4:
        return None

    index_status = line[0]
    worktree_status = line[1]
    file_path = line[3:]

    # Determine overall status
    if index_status == "?" and worktree_status == "?":
        status = "??"
        staged = False
    elif index_status != " " and index_status != "?":
        status = index_status
        staged = True
    else:
        status = worktree_status
        staged = False

    return GitStatusFile(status=status, path=file_path, staged=staged)


@router.get("/git/status", response_model=GitStatus)
async def get_git_status(
    dir: str = Query(default=".", description="Git repository directory"),
) -> GitStatus:
    """Get git repository status.

    Args:
        dir: Repository directory

    Returns:
        Git status including branch, ahead/behind, and file statuses
    """
    try:
        # Resolve directory
        if dir == ".":
            cwd = os.getcwd()
        else:
            cwd = str(Path(dir).resolve())

        # Check if it's a git repo
        stdout, stderr, code = await _run_git(["rev-parse", "--git-dir"], cwd)
        if code != 0:
            return GitStatus(branch="", ahead=0, behind=0, files=[], isRepo=False)

        # Get current branch
        stdout, _, _ = await _run_git(["branch", "--show-current"], cwd)
        branch = stdout.strip() or "HEAD"

        # Get ahead/behind
        ahead = 0
        behind = 0
        stdout, _, code = await _run_git(
            ["rev-list", "--left-right", "--count", f"{branch}...@{{upstream}}"],
            cwd,
        )
        if code == 0:
            parts = stdout.strip().split()
            if len(parts) == 2:
                ahead = int(parts[0])
                behind = int(parts[1])

        # Get file statuses
        stdout, _, _ = await _run_git(["status", "--porcelain"], cwd)
        files: list[GitStatusFile] = []
        for line in stdout.split("\n"):
            if line:
                status_file = _parse_status_line(line)
                if status_file:
                    files.append(status_file)

        return GitStatus(
            branch=branch,
            ahead=ahead,
            behind=behind,
            files=files,
            isRepo=True,
        )
    except Exception as e:
        logger.error(f"Failed to get git status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/git/log", response_model=GitLogResponse)
async def get_git_log(
    dir: str = Query(default=".", description="Git repository directory"),
    limit: int = Query(default=50, description="Maximum number of entries"),
) -> GitLogResponse:
    """Get git log entries.

    Args:
        dir: Repository directory
        limit: Maximum number of log entries

    Returns:
        List of git log entries
    """
    try:
        # Resolve directory
        if dir == ".":
            cwd = os.getcwd()
        else:
            cwd = str(Path(dir).resolve())

        # Get log with custom format
        # Format: hash|shortHash|author|date|message
        stdout, stderr, code = await _run_git(
            [
                "log",
                f"-{limit}",
                "--format=%H|%h|%an|%aI|%s",
            ],
            cwd,
        )

        if code != 0:
            # Might be empty repo
            return GitLogResponse(entries=[])

        entries: list[GitLogEntry] = []
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 4)
            if len(parts) >= 5:
                entries.append(GitLogEntry(
                    hash=parts[0],
                    shortHash=parts[1],
                    author=parts[2],
                    date=parts[3],
                    message=parts[4],
                ))

        return GitLogResponse(entries=entries)
    except Exception as e:
        logger.error(f"Failed to get git log: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/git/diff", response_model=GitDiffResponse)
async def get_git_diff(
    dir: str = Query(default=".", description="Git repository directory"),
    staged: bool = Query(default=False, description="Show staged diff"),
    file: str | None = Query(default=None, description="Specific file to diff"),
) -> GitDiffResponse:
    """Get git diff.

    Args:
        dir: Repository directory
        staged: If True, show staged changes (--cached)
        file: Specific file to diff (optional)

    Returns:
        Git diff output
    """
    try:
        # Resolve directory
        if dir == ".":
            cwd = os.getcwd()
        else:
            cwd = str(Path(dir).resolve())

        # Build command
        args = ["diff", "--no-color"]
        if staged:
            args.append("--cached")
        if file:
            args.append("--")
            args.append(file)

        stdout, stderr, code = await _run_git(args, cwd)

        return GitDiffResponse(diff=stdout)
    except Exception as e:
        logger.error(f"Failed to get git diff: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/git/commit", response_model=GitCommitResponse)
async def git_commit(request: GitCommitRequest) -> GitCommitResponse:
    """Create a git commit.

    Args:
        request: Commit request with message and optional files

    Returns:
        Commit result
    """
    try:
        # Resolve directory
        if request.dir == ".":
            cwd = os.getcwd()
        else:
            cwd = str(Path(request.dir).resolve())

        # Stage files if specified
        if request.files:
            for file in request.files:
                stdout, stderr, code = await _run_git(["add", file], cwd)
                if code != 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to stage {file}: {stderr}",
                    )

        # Create commit
        stdout, stderr, code = await _run_git(
            ["commit", "-m", request.message],
            cwd,
        )

        if code != 0:
            # Check if nothing to commit
            if "nothing to commit" in stdout or "nothing to commit" in stderr:
                return GitCommitResponse(
                    success=False,
                    message="Nothing to commit",
                )
            raise HTTPException(
                status_code=400,
                detail=f"Failed to commit: {stderr or stdout}",
            )

        # Get commit hash
        stdout, _, _ = await _run_git(["rev-parse", "HEAD"], cwd)
        commit_hash = stdout.strip()

        return GitCommitResponse(
            success=True,
            hash=commit_hash,
            message=request.message,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to commit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/git/stage")
async def git_stage(
    dir: str = Query(default=".", description="Git repository directory"),
    file: str = Query(..., description="File to stage"),
) -> dict:
    """Stage a file for commit.

    Args:
        dir: Repository directory
        file: File to stage

    Returns:
        Success confirmation
    """
    try:
        # Resolve directory
        if dir == ".":
            cwd = os.getcwd()
        else:
            cwd = str(Path(dir).resolve())

        stdout, stderr, code = await _run_git(["add", file], cwd)

        if code != 0:
            raise HTTPException(status_code=400, detail=f"Failed to stage: {stderr}")

        return {"success": True, "file": file}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to stage file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/git/unstage")
async def git_unstage(
    dir: str = Query(default=".", description="Git repository directory"),
    file: str = Query(..., description="File to unstage"),
) -> dict:
    """Unstage a file.

    Args:
        dir: Repository directory
        file: File to unstage

    Returns:
        Success confirmation
    """
    try:
        # Resolve directory
        if dir == ".":
            cwd = os.getcwd()
        else:
            cwd = str(Path(dir).resolve())

        stdout, stderr, code = await _run_git(["reset", "HEAD", file], cwd)

        if code != 0:
            raise HTTPException(status_code=400, detail=f"Failed to unstage: {stderr}")

        return {"success": True, "file": file}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to unstage file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/git/discard")
async def git_discard(
    dir: str = Query(default=".", description="Git repository directory"),
    file: str = Query(..., description="File to discard changes"),
) -> dict:
    """Discard changes to a file.

    Args:
        dir: Repository directory
        file: File to discard changes

    Returns:
        Success confirmation
    """
    try:
        # Resolve directory
        if dir == ".":
            cwd = os.getcwd()
        else:
            cwd = str(Path(dir).resolve())

        stdout, stderr, code = await _run_git(["checkout", "--", file], cwd)

        if code != 0:
            raise HTTPException(status_code=400, detail=f"Failed to discard: {stderr}")

        return {"success": True, "file": file}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to discard changes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/git/branches")
async def get_git_branches(
    dir: str = Query(default=".", description="Git repository directory"),
) -> dict:
    """Get git branches.

    Args:
        dir: Repository directory

    Returns:
        List of branches and current branch
    """
    try:
        # Resolve directory
        if dir == ".":
            cwd = os.getcwd()
        else:
            cwd = str(Path(dir).resolve())

        # Get all branches
        stdout, stderr, code = await _run_git(["branch", "-a"], cwd)

        branches: list[dict] = []
        current = ""

        for line in stdout.split("\n"):
            line = line.strip()
            if not line:
                continue

            is_current = line.startswith("*")
            name = line.lstrip("* ").strip()

            if is_current:
                current = name

            # Skip remote HEAD pointers
            if "->" in name:
                continue

            branches.append({
                "name": name,
                "isRemote": name.startswith("remotes/"),
                "isCurrent": is_current,
            })

        return {"branches": branches, "current": current}
    except Exception as e:
        logger.error(f"Failed to get branches: {e}")
        raise HTTPException(status_code=500, detail=str(e))
