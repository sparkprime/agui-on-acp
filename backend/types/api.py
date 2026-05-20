"""REST API type definitions for the ACP → AG-UI Bridge backend."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .acp import ModeInfo, ModesInfo


# =============================================================================
# Session Types
# =============================================================================


class Session(BaseModel):
    """Session information returned by REST API."""

    id: str
    cwd: str
    status: Literal["idle", "working", "awaiting_approval"] = "idle"
    model: str | None = None
    mode: str | None = None
    createdAt: str  # ISO 8601 datetime string


class CreateSessionRequest(BaseModel):
    """Request body for creating a new session."""

    cwd: str


class CreateSessionResponse(BaseModel):
    """Response body for session creation."""

    session: Session
    modes: ModesInfo | None = None


class ListSessionsResponse(BaseModel):
    """Response body for listing sessions."""

    sessions: list[Session] = Field(default_factory=list)


# =============================================================================
# File Types
# =============================================================================


class FileItem(BaseModel):
    """File or directory item in a listing."""

    name: str
    path: str
    isDirectory: bool
    size: int | None = None
    modifiedTime: float | None = None  # Unix timestamp


class ListFilesRequest(BaseModel):
    """Query parameters for listing files."""

    path: str = "."


class ListFilesResponse(BaseModel):
    """Response body for file listing."""

    items: list[FileItem] = Field(default_factory=list)
    path: str


class ReadFileRequest(BaseModel):
    """Query parameters for reading a file."""

    path: str


class ReadFileResponse(BaseModel):
    """Response body for file content."""

    content: str
    path: str


class WriteFileRequest(BaseModel):
    """Request body for writing a file."""

    path: str
    content: str


class WriteFileResponse(BaseModel):
    """Response body for write confirmation."""

    success: bool
    path: str


class DeleteFileRequest(BaseModel):
    """Query parameters for deleting a file."""

    path: str


class DeleteFileResponse(BaseModel):
    """Response body for delete confirmation."""

    success: bool
    path: str


# =============================================================================
# Git Types
# =============================================================================


class GitStatusFile(BaseModel):
    """File status in Git."""

    status: str  # e.g., "M", "A", "D", "??"
    path: str
    staged: bool = False


class GitStatus(BaseModel):
    """Git repository status."""

    branch: str
    ahead: int = 0
    behind: int = 0
    files: list[GitStatusFile] = Field(default_factory=list)
    isRepo: bool = True


class GitStatusRequest(BaseModel):
    """Query parameters for Git status."""

    dir: str = "."


class GitLogEntry(BaseModel):
    """Git log entry."""

    hash: str
    shortHash: str
    message: str
    author: str
    date: str  # ISO 8601 datetime string


class GitLogRequest(BaseModel):
    """Query parameters for Git log."""

    dir: str = "."
    limit: int = 50


class GitLogResponse(BaseModel):
    """Response body for Git log."""

    entries: list[GitLogEntry] = Field(default_factory=list)


class GitDiffRequest(BaseModel):
    """Query parameters for Git diff."""

    dir: str = "."
    staged: bool = False


class GitDiffResponse(BaseModel):
    """Response body for Git diff."""

    diff: str


class GitCommitRequest(BaseModel):
    """Request body for Git commit."""

    dir: str = "."
    message: str
    files: list[str] | None = None  # None = all staged files


class GitCommitResponse(BaseModel):
    """Response body for Git commit."""

    success: bool
    hash: str | None = None
    message: str | None = None


# =============================================================================
# Terminal Types
# =============================================================================


class CreateTerminalRequest(BaseModel):
    """Request body for creating a terminal."""

    cwd: str = "."
    cols: int = 80
    rows: int = 24


class CreateTerminalResponse(BaseModel):
    """Response body for terminal creation."""

    terminalId: str


class TerminalInfo(BaseModel):
    """Terminal information."""

    id: str
    cwd: str
    active: bool = True


class ListTerminalsResponse(BaseModel):
    """Response body for listing terminals."""

    terminals: list[TerminalInfo] = Field(default_factory=list)


# =============================================================================
# Health Check
# =============================================================================


class HealthResponse(BaseModel):
    """Response body for health check."""

    status: Literal["ok"] = "ok"
    version: str
    project: str = "acp-to-agui"
