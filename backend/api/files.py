"""File operations REST API routes."""

import logging
import os
from pathlib import Path

import aiofiles
from fastapi import APIRouter, HTTPException, Query

from backend.types.api import (
    DeleteFileResponse,
    FileItem,
    ListFilesResponse,
    ReadFileResponse,
    WriteFileRequest,
    WriteFileResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_safe_path(base: str, path: str) -> Path:
    """Get a safe path that doesn't escape the base directory.

    Args:
        base: Base directory
        path: Relative path

    Returns:
        Resolved absolute path

    Raises:
        HTTPException: If path tries to escape base directory
    """
    base_path = Path(base).resolve()
    full_path = (base_path / path).resolve()

    # Ensure path doesn't escape base directory
    try:
        full_path.relative_to(base_path)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Path escapes base directory",
        )

    return full_path


@router.get("/files", response_model=ListFilesResponse)
async def list_files(
    path: str = Query(default=".", description="Directory path to list"),
    base: str = Query(default=".", description="Base directory"),
) -> ListFilesResponse:
    """List files and directories in a path.

    Args:
        path: Relative path to list
        base: Base directory (defaults to cwd)

    Returns:
        List of files and directories
    """
    try:
        # If base is ".", use current working directory
        if base == ".":
            base = os.getcwd()

        full_path = _get_safe_path(base, path)

        if not full_path.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")

        if not full_path.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {path}")

        items: list[FileItem] = []
        for entry in full_path.iterdir():
            try:
                stat = entry.stat()
                items.append(FileItem(
                    name=entry.name,
                    path=str(entry.relative_to(Path(base).resolve())),
                    isDirectory=entry.is_dir(),
                    size=stat.st_size if entry.is_file() else None,
                    modifiedTime=stat.st_mtime,
                ))
            except (OSError, PermissionError) as e:
                logger.warning(f"Cannot stat {entry}: {e}")
                continue

        # Sort directories first, then files
        items.sort(key=lambda x: (not x.isDirectory, x.name.lower()))

        return ListFilesResponse(items=items, path=path)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list files: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/files/content", response_model=ReadFileResponse)
async def read_file(
    path: str = Query(..., description="File path to read"),
    base: str = Query(default=".", description="Base directory"),
) -> ReadFileResponse:
    """Read the contents of a file.

    Args:
        path: Relative path to file
        base: Base directory (defaults to cwd)

    Returns:
        File content
    """
    try:
        # If base is ".", use current working directory
        if base == ".":
            base = os.getcwd()

        full_path = _get_safe_path(base, path)

        if not full_path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {path}")

        if not full_path.is_file():
            raise HTTPException(status_code=400, detail=f"Not a file: {path}")

        async with aiofiles.open(full_path, "r", encoding="utf-8") as f:
            content = await f.read()

        return ReadFileResponse(content=content, path=path)
    except HTTPException:
        raise
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid UTF-8 text")
    except Exception as e:
        logger.error(f"Failed to read file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/files", response_model=WriteFileResponse)
async def create_file(
    request: WriteFileRequest,
    base: str = Query(default=".", description="Base directory"),
) -> WriteFileResponse:
    """Create a new file.

    Args:
        request: File creation request
        base: Base directory (defaults to cwd)

    Returns:
        Success confirmation
    """
    try:
        # If base is ".", use current working directory
        if base == ".":
            base = os.getcwd()

        full_path = _get_safe_path(base, request.path)

        if full_path.exists():
            raise HTTPException(
                status_code=409,
                detail=f"File already exists: {request.path}",
            )

        # Create parent directories if needed
        full_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiofiles.open(full_path, "w", encoding="utf-8") as f:
            await f.write(request.content)

        return WriteFileResponse(success=True, path=request.path)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/files", response_model=WriteFileResponse)
async def update_file(
    request: WriteFileRequest,
    base: str = Query(default=".", description="Base directory"),
) -> WriteFileResponse:
    """Update an existing file.

    Args:
        request: File update request
        base: Base directory (defaults to cwd)

    Returns:
        Success confirmation
    """
    try:
        # If base is ".", use current working directory
        if base == ".":
            base = os.getcwd()

        full_path = _get_safe_path(base, request.path)

        if not full_path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {request.path}")

        if not full_path.is_file():
            raise HTTPException(status_code=400, detail=f"Not a file: {request.path}")

        async with aiofiles.open(full_path, "w", encoding="utf-8") as f:
            await f.write(request.content)

        return WriteFileResponse(success=True, path=request.path)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/files", response_model=DeleteFileResponse)
async def delete_file(
    path: str = Query(..., description="File path to delete"),
    base: str = Query(default=".", description="Base directory"),
) -> DeleteFileResponse:
    """Delete a file.

    Args:
        path: Relative path to file
        base: Base directory (defaults to cwd)

    Returns:
        Success confirmation
    """
    try:
        # If base is ".", use current working directory
        if base == ".":
            base = os.getcwd()

        full_path = _get_safe_path(base, path)

        if not full_path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {path}")

        if full_path.is_dir():
            # Recursively delete directory
            import shutil
            shutil.rmtree(full_path)
        else:
            full_path.unlink()

        return DeleteFileResponse(success=True, path=path)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/files/mkdir")
async def create_directory(
    path: str = Query(..., description="Directory path to create"),
    base: str = Query(default=".", description="Base directory"),
) -> dict:
    """Create a directory.

    Args:
        path: Relative path for new directory
        base: Base directory (defaults to cwd)

    Returns:
        Success confirmation
    """
    try:
        # If base is ".", use current working directory
        if base == ".":
            base = os.getcwd()

        full_path = _get_safe_path(base, path)

        if full_path.exists():
            raise HTTPException(
                status_code=409,
                detail=f"Path already exists: {path}",
            )

        full_path.mkdir(parents=True)

        return {"success": True, "path": path}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create directory: {e}")
        raise HTTPException(status_code=500, detail=str(e))
