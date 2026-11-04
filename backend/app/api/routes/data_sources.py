from app.services.tenant_ownership import RowRef
from app.services.tenant_responses import safe_model_read
from app.services.tenant_mutations import MutationIntent, write_guard
from uuid import uuid4
from io import BytesIO

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
from app.services.media_storage import MediaPathError, media_relative_path, normalize_upload_filename, write_media_upload
from app.services.tenancy import (
    related_client_matches,
    apply_client_filter,
    assert_client_exists,
    can_write_client_scope,
    get_data_source_in_scope,
    get_dataset_in_scope,
    require_client_scope,
)


from app.services.tenant_responses import read_snapshot
from app.services.tenant_queries import readable_query

router = APIRouter()
settings = get_settings()


def safe_dataset_read(dataset: Dataset, db: Session) -> DatasetRead:
    return safe_model_read(dataset, DatasetRead, db)


def get_readable_dataset(db: Session, dataset_id: str, current_user: User) -> Dataset:
    dataset = get_dataset_in_scope(db, dataset_id, current_user)
    source = db.get(DataSource, dataset.data_source_id)
    if source is None or source.client_id != dataset.client_id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


def serialize_dataset_preview(dataset: Dataset, import_record: DatasetImport | None, columns: list[DatasetColumn], rows: list[DatasetRow], db: Session) -> dict:
    return {
        "dataset": safe_dataset_read(dataset, db).model_dump(),
        "current_import": DatasetImportRead.model_validate(import_record).model_dump() if import_record else None,
        "columns": [DatasetColumnRead.model_validate(column).model_dump() for column in columns],
        "rows": [DatasetRowRead.model_validate(row).model_dump() for row in rows],
    }


def get_import_in_scope(dataset: Dataset, import_id: str | None, db: Session) -> DatasetImport | None:
    if import_id:
        import_record = db.scalar(readable_query(select(DatasetImport), DatasetImport, db=db).where(DatasetImport.id == import_id))
        if not import_record or import_record.dataset_id != dataset.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset import not found")
        return import_record
    if dataset.current_import_id:
        record = db.scalar(readable_query(select(DatasetImport), DatasetImport, db=db).where(DatasetImport.id == dataset.current_import_id))
        return record if record and record.dataset_id == dataset.id else None
    return None


@router.get("/data-sources", response_model=list[DataSourceRead])
@read_snapshot(response_model=list[DataSourceRead])
def list_data_sources(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DataSource]:
    query = apply_client_filter(readable_query(select(DataSource), DataSource, db=db).order_by(DataSource.created_at.desc()), DataSource, current_user)
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

    values = dict(
        client_id=client_id,
        name=payload.name,
        source_type=payload.source_type,
        description=payload.description,
        config_json=payload.config_json,
        is_active=payload.is_active,
    )
    ref = RowRef("DataSource", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=client_id, operation="create_data_source", intents=(intent,)):
        data_source = DataSource(id=ref.identity, **values)
        db.add(data_source)
        db.commit()
        db.refresh(data_source)
        return data_source


@router.get("/datasets", response_model=list[DatasetRead])
@read_snapshot(response_model=list[DatasetRead])
def list_datasets(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Dataset]:
    query = apply_client_filter(readable_query(select(Dataset), Dataset, db=db).where(
        related_client_matches(DataSource, Dataset.data_source_id, Dataset.client_id)
    ).order_by(Dataset.created_at.desc()), Dataset, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(Dataset.client_id == target_client_id)
    return [safe_dataset_read(dataset, db) for dataset in db.scalars(query)]


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

    values = dict(
        client_id=client_id,
        data_source_id=data_source.id,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
    )
    ref = RowRef("Dataset", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=client_id, operation="create_dataset", intents=(intent,)):
        dataset = Dataset(id=ref.identity, **values)
        db.add(dataset)
        db.commit()
        db.refresh(dataset)
        return dataset


@router.get("/datasets/{dataset_id}", response_model=DatasetRead)
@read_snapshot(response_model=DatasetRead)
def get_dataset(
    dataset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dataset:
    return safe_dataset_read(get_readable_dataset(db, dataset_id, current_user), db)


@router.get("/datasets/{dataset_id}/imports", response_model=list[DatasetImportRead])
@read_snapshot(response_model=list[DatasetImportRead])
def list_dataset_imports(
    dataset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DatasetImport]:
    dataset = get_readable_dataset(db, dataset_id, current_user)
    return list(
        db.scalars(
            readable_query(select(DatasetImport), DatasetImport, db=db)
            .where(DatasetImport.dataset_id == dataset.id)
            .order_by(DatasetImport.imported_at.desc())
        )
    )


@router.get("/datasets/{dataset_id}/columns", response_model=list[DatasetColumnRead])
@read_snapshot(response_model=list[DatasetColumnRead])
def list_dataset_columns(
    dataset_id: str,
    import_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DatasetColumn]:
    dataset = get_readable_dataset(db, dataset_id, current_user)
    import_record = get_import_in_scope(dataset, import_id, db)
    if not import_record:
        return []
    return list(
        db.scalars(
            readable_query(select(DatasetColumn), DatasetColumn, db=db)
            .where(DatasetColumn.import_id == import_record.id, DatasetColumn.dataset_id == dataset.id)
            .order_by(DatasetColumn.position_index.asc())
        )
    )


@router.get("/datasets/{dataset_id}/rows", response_model=list[DatasetRowRead])
@read_snapshot(response_model=list[DatasetRowRead])
def list_dataset_rows(
    dataset_id: str,
    import_id: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DatasetRow]:
    dataset = get_readable_dataset(db, dataset_id, current_user)
    import_record = get_import_in_scope(dataset, import_id, db)
    if not import_record:
        return []
    return list(
        db.scalars(
            readable_query(select(DatasetRow), DatasetRow, db=db)
            .where(DatasetRow.import_id == import_record.id, DatasetRow.dataset_id == dataset.id)
            .order_by(DatasetRow.row_index.asc())
            .offset(offset)
            .limit(limit)
        )
    )


@router.get("/datasets/{dataset_id}/preview")
@read_snapshot()
def get_dataset_preview(
    dataset_id: str,
    import_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    dataset = get_readable_dataset(db, dataset_id, current_user)
    import_record = get_import_in_scope(dataset, import_id, db)
    if not import_record:
        return serialize_dataset_preview(dataset, None, [], [], db)

    columns = list(
        db.scalars(
            readable_query(select(DatasetColumn), DatasetColumn, db=db)
            .where(DatasetColumn.import_id == import_record.id, DatasetColumn.dataset_id == dataset.id)
            .order_by(DatasetColumn.position_index.asc())
        )
    )
    rows = list(
        db.scalars(
            readable_query(select(DatasetRow), DatasetRow, db=db)
            .where(DatasetRow.import_id == import_record.id, DatasetRow.dataset_id == dataset.id)
            .order_by(DatasetRow.row_index.asc())
            .limit(limit)
        )
    )
    return serialize_dataset_preview(dataset, import_record, columns, rows, db)


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
    try:
        source_filename = normalize_upload_filename(file.filename)
    except MediaPathError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc

    # Parsing and complete row proposals are prepared without business rows or
    # final storage. The source stream contains only the submitted QA/user file.
    file_bytes = file.file.read()
    parse_error = None
    try:
        snapshot = extract_tabular_snapshot(source_filename, file.content_type, file_bytes)
    except ValueError as exc:
        parse_error = str(exc)
        snapshot = None
    ref = RowRef("DatasetImport", str(uuid4()))
    import_values = dict(
        dataset_id=dataset.id,
        source_filename=source_filename,
        source_mime_type=file.content_type,
        storage_path="",
        import_status=DatasetImportStatus.FAILED if parse_error else DatasetImportStatus.COMPLETED,
    )
    dataset_values = {"status": DatasetStatus.ERROR if parse_error else DatasetStatus.READY}
    columns, rows = [], []
    if parse_error:
        import_values["summary_json"] = {"error": parse_error}
    else:
        import_values.update({key: snapshot[key] for key in ("detected_sheet_name", "row_count", "column_count", "summary_json")})
        dataset_values.update(current_import_id=ref.identity, row_count=snapshot["row_count"], column_count=snapshot["column_count"])
        for column in snapshot["columns"]:
            columns.append((str(uuid4()), dict(
                dataset_id=dataset.id,
                import_id=ref.identity,
                column_key=column["column_key"],
                display_name=column["display_name"],
                source_name=column["source_name"],
                data_type=column["data_type"] if isinstance(column["data_type"], DatasetColumnType) else DatasetColumnType(column["data_type"]),
                position_index=column["position_index"],
                sample_value=column["sample_value"],
                is_visible=column["is_visible"],
            )))
        for row in snapshot["rows"]:
            rows.append((str(uuid4()), dict(
                dataset_id=dataset.id,
                import_id=ref.identity,
                row_index=row["row_index"],
                row_data_json=row["row_data_json"],
                row_hash=row["row_hash"],
            )))
    intents = (MutationIntent("UPDATE", RowRef("Dataset", dataset.id), tuple(dataset_values.items())),
               MutationIntent("CREATE", ref, tuple(import_values.items())))
    intents += tuple(MutationIntent("CREATE", RowRef("DatasetColumn", identity), tuple(values.items())) for identity, values in columns)
    intents += tuple(MutationIntent("CREATE", RowRef("DatasetRow", identity), tuple(values.items())) for identity, values in rows)
    with write_guard(db, actor=current_user, client_id=dataset.client_id,
                     operation="upload_dataset_import", intents=intents):
        try:
            uploaded = write_media_upload(settings.MEDIA_ROOT, ("datasets", dataset.client_id, dataset.id, ref.identity),
                                          source_filename, file.content_type, BytesIO(file_bytes))
            import_values["storage_path"] = media_relative_path(settings.MEDIA_ROOT, uploaded.physical_path)
        except (MediaPathError, OSError) as exc:
            db.rollback()
            code = exc.code if isinstance(exc, MediaPathError) else "UPLOAD_WRITE_FAILED"
            raise HTTPException(status_code=400, detail=code) from exc
        # This final path comes exclusively from backend storage, never request
        # JSON. All tenant/parent references were approved before creating it.
        import_record = DatasetImport(id=ref.identity, **import_values)
        db.add(import_record)
        db.flush()
        for identity, values in columns:
            db.add(DatasetColumn(id=identity, **values))
        for identity, values in rows:
            db.add(DatasetRow(id=identity, **values))
        dataset.status = dataset_values["status"]
        if not parse_error:
            dataset.current_import_id = ref.identity
            dataset.row_count = snapshot["row_count"]
            dataset.column_count = snapshot["column_count"]
        db.commit()
        if parse_error:
            raise HTTPException(status_code=400, detail=parse_error)
        db.refresh(import_record)
        return import_record
