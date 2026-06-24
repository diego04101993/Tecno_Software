from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.models.entities import (
    DataSource,
    Dataset,
    DatasetColumn,
    DatasetColumnType,
    DatasetImport,
    DatasetImportStatus,
    DatasetRow,
    DatasetStatus,
    User,
)
from app.schemas.domain import (
    DataSourceCreate,
    DataSourceRead,
    DatasetColumnRead,
    DatasetCreate,
    DatasetImportRead,
    DatasetRead,
    DatasetRowRead,
)
from app.services.dataset_ingest import extract_tabular_snapshot
from app.services.tenancy import (
    apply_client_filter,
    assert_client_exists,
    can_write_client_scope,
    get_data_source_in_scope,
    get_dataset_in_scope,
    require_client_scope,
)


router = APIRouter()
settings = get_settings()


def serialize_dataset_preview(dataset: Dataset, import_record: DatasetImport | None, columns: list[DatasetColumn], rows: list[DatasetRow]) -> dict:
    return {
        "dataset": DatasetRead.model_validate(dataset).model_dump(),
        "current_import": DatasetImportRead.model_validate(import_record).model_dump() if import_record else None,
        "columns": [DatasetColumnRead.model_validate(column).model_dump() for column in columns],
        "rows": [DatasetRowRead.model_validate(row).model_dump() for row in rows],
    }


def get_import_in_scope(dataset: Dataset, import_id: str | None, db: Session) -> DatasetImport | None:
    if import_id:
        import_record = db.get(DatasetImport, import_id)
        if not import_record or import_record.dataset_id != dataset.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset import not found")
        return import_record
    if dataset.current_import_id:
        return db.get(DatasetImport, dataset.current_import_id)
    return None


@router.get("/data-sources", response_model=list[DataSourceRead])
def list_data_sources(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DataSource]:
    query = apply_client_filter(select(DataSource).order_by(DataSource.created_at.desc()), DataSource, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(DataSource.client_id == target_client_id)
    return list(db.scalars(query))


@router.post("/data-sources", response_model=DataSourceRead, status_code=status.HTTP_201_CREATED)
def create_data_source(
    payload: DataSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DataSource:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create data sources")

    client_id = require_client_scope(current_user, payload.client_id)
    assert_client_exists(db, client_id)

    data_source = DataSource(
        client_id=client_id,
        name=payload.name,
        source_type=payload.source_type,
        description=payload.description,
        config_json=payload.config_json,
        is_active=payload.is_active,
    )
    db.add(data_source)
    db.commit()
    db.refresh(data_source)
    return data_source


@router.get("/datasets", response_model=list[DatasetRead])
def list_datasets(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Dataset]:
    query = apply_client_filter(select(Dataset).order_by(Dataset.created_at.desc()), Dataset, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(Dataset.client_id == target_client_id)
    return list(db.scalars(query))


@router.post("/datasets", response_model=DatasetRead, status_code=status.HTTP_201_CREATED)
def create_dataset(
    payload: DatasetCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dataset:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create datasets")

    client_id = require_client_scope(current_user, payload.client_id)
    assert_client_exists(db, client_id)
    data_source = get_data_source_in_scope(db, payload.data_source_id, current_user)
    if data_source.client_id != client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data source not found for this client")

    existing = db.scalar(select(Dataset).where(Dataset.client_id == client_id, Dataset.slug == payload.slug))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A dataset with this slug already exists for the client")

    dataset = Dataset(
        client_id=client_id,
        data_source_id=data_source.id,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset


@router.get("/datasets/{dataset_id}", response_model=DatasetRead)
def get_dataset(
    dataset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dataset:
    return get_dataset_in_scope(db, dataset_id, current_user)


@router.get("/datasets/{dataset_id}/imports", response_model=list[DatasetImportRead])
def list_dataset_imports(
    dataset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DatasetImport]:
    dataset = get_dataset_in_scope(db, dataset_id, current_user)
    return list(
        db.scalars(
            select(DatasetImport)
            .where(DatasetImport.dataset_id == dataset.id)
            .order_by(DatasetImport.imported_at.desc())
        )
    )


@router.get("/datasets/{dataset_id}/columns", response_model=list[DatasetColumnRead])
def list_dataset_columns(
    dataset_id: str,
    import_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DatasetColumn]:
    dataset = get_dataset_in_scope(db, dataset_id, current_user)
    import_record = get_import_in_scope(dataset, import_id, db)
    if not import_record:
        return []
    return list(
        db.scalars(
            select(DatasetColumn)
            .where(DatasetColumn.import_id == import_record.id)
            .order_by(DatasetColumn.position_index.asc())
        )
    )


@router.get("/datasets/{dataset_id}/rows", response_model=list[DatasetRowRead])
def list_dataset_rows(
    dataset_id: str,
    import_id: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DatasetRow]:
    dataset = get_dataset_in_scope(db, dataset_id, current_user)
    import_record = get_import_in_scope(dataset, import_id, db)
    if not import_record:
        return []
    return list(
        db.scalars(
            select(DatasetRow)
            .where(DatasetRow.import_id == import_record.id)
            .order_by(DatasetRow.row_index.asc())
            .offset(offset)
            .limit(limit)
        )
    )


@router.get("/datasets/{dataset_id}/preview")
def get_dataset_preview(
    dataset_id: str,
    import_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    dataset = get_dataset_in_scope(db, dataset_id, current_user)
    import_record = get_import_in_scope(dataset, import_id, db)
    if not import_record:
        return serialize_dataset_preview(dataset, None, [], [])

    columns = list(
        db.scalars(
            select(DatasetColumn)
            .where(DatasetColumn.import_id == import_record.id)
            .order_by(DatasetColumn.position_index.asc())
        )
    )
    rows = list(
        db.scalars(
            select(DatasetRow)
            .where(DatasetRow.import_id == import_record.id)
            .order_by(DatasetRow.row_index.asc())
            .limit(limit)
        )
    )
    return serialize_dataset_preview(dataset, import_record, columns, rows)


@router.post("/datasets/{dataset_id}/imports/upload", response_model=DatasetImportRead, status_code=status.HTTP_201_CREATED)
def upload_dataset_import(
    dataset_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DatasetImport:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to import datasets")

    dataset = get_dataset_in_scope(db, dataset_id, current_user)
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A file name is required")

    dataset.status = DatasetStatus.PROCESSING
    import_record = DatasetImport(
        dataset_id=dataset.id,
        source_filename=file.filename,
        source_mime_type=file.content_type,
        storage_path="",
        import_status=DatasetImportStatus.PENDING,
    )
    db.add(import_record)
    db.flush()

    target_dir = settings.MEDIA_ROOT / "datasets" / dataset.client_id / dataset.id / import_record.id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / Path(file.filename).name
    file_bytes = file.file.read()
    target_path.write_bytes(file_bytes)
    import_record.storage_path = str(target_path.relative_to(settings.MEDIA_ROOT))

    try:
        snapshot = extract_tabular_snapshot(file.filename, file.content_type, file_bytes)
    except ValueError as next_error:
        import_record.import_status = DatasetImportStatus.FAILED
        import_record.summary_json = {"error": str(next_error)}
        dataset.status = DatasetStatus.ERROR
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(next_error)) from next_error

    import_record.import_status = DatasetImportStatus.COMPLETED
    import_record.detected_sheet_name = snapshot["detected_sheet_name"]
    import_record.row_count = snapshot["row_count"]
    import_record.column_count = snapshot["column_count"]
    import_record.summary_json = snapshot["summary_json"]

    for column in snapshot["columns"]:
        db.add(
            DatasetColumn(
                dataset_id=dataset.id,
                import_id=import_record.id,
                column_key=column["column_key"],
                display_name=column["display_name"],
                source_name=column["source_name"],
                data_type=column["data_type"] if isinstance(column["data_type"], DatasetColumnType) else DatasetColumnType(column["data_type"]),
                position_index=column["position_index"],
                sample_value=column["sample_value"],
                is_visible=column["is_visible"],
            )
        )

    for row in snapshot["rows"]:
        db.add(
            DatasetRow(
                dataset_id=dataset.id,
                import_id=import_record.id,
                row_index=row["row_index"],
                row_data_json=row["row_data_json"],
                row_hash=row["row_hash"],
            )
        )

    dataset.status = DatasetStatus.READY
    dataset.current_import_id = import_record.id
    dataset.row_count = import_record.row_count
    dataset.column_count = import_record.column_count

    db.commit()
    db.refresh(import_record)
    return import_record
