"""Backup routes under ``/api/v1``."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from portfolio_manager.api.dependencies import get_container
from portfolio_manager.bootstrap import Container
from portfolio_manager.infrastructure.db import backup as backup_mod
from portfolio_manager.infrastructure.db import restore as restore_mod

router = APIRouter(prefix="/api/v1", tags=["backups"])


class BackupItem(BaseModel):
    """A backup file."""

    name: str
    created_at: str
    size_bytes: int


class BackupList(BaseModel):
    """Backups, newest first, the folder that holds them, and any restore waiting."""

    folder: str
    backups: list[BackupItem]
    pending_restore: str | None = None


class RestoreRequest(BaseModel):
    """Which backup to restore at the next launch."""

    name: str


class RestoreResponse(BaseModel):
    """Outcome of a restore request."""

    name: str
    restart_required: bool = True


class ExportRequest(BaseModel):
    """Folder to export a copy of the database into."""

    destination: str


class ExportResponse(BaseModel):
    """The file that was written."""

    path: str


def _item(b: backup_mod.BackupInfo) -> BackupItem:
    return BackupItem(name=b.name, created_at=b.created_at.isoformat(), size_bytes=b.size_bytes)


@router.get("/backups", response_model=BackupList)
async def list_backups(container: Container = Depends(get_container)) -> BackupList:  # noqa: B008
    """List the available backups."""
    db = container.db
    return BackupList(
        folder=str(backup_mod.backups_dir(db.path)),
        backups=[_item(b) for b in backup_mod.list_backups(db.path)],
        pending_restore=restore_mod.pending_restore(db.path),
    )


@router.post("/backups", response_model=BackupItem, status_code=201)
async def create_backup(container: Container = Depends(get_container)) -> BackupItem:  # noqa: B008
    """Make a backup now."""
    return _item(backup_mod.create_backup(container.db))


@router.post("/backups/restore", response_model=RestoreResponse)
async def restore_backup(
    body: RestoreRequest,
    container: Container = Depends(get_container),  # noqa: B008
) -> RestoreResponse:
    """Request that a backup be restored. It is applied when the app next starts."""
    restore_mod.request_restore(container.db.path, body.name)
    return RestoreResponse(name=body.name)


@router.post("/backups/export", response_model=ExportResponse)
async def export_database(
    body: ExportRequest,
    container: Container = Depends(get_container),  # noqa: B008
) -> ExportResponse:
    """Write a verified copy of the database into a folder the user chose."""
    return ExportResponse(path=str(backup_mod.export_backup(container.db, body.destination)))
