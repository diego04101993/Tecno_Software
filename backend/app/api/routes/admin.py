from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_super_admin
from app.models.entities import User
from app.services.cleanup_retention import run_retention_cleanup


router = APIRouter()


@router.post("/cleanup/run")
def run_cleanup(
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
) -> dict[str, object]:
    return run_retention_cleanup(db)
