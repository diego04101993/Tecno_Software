from datetime import date, datetime, time
from enum import Enum
import secrets

from sqlalchemy import Boolean, Date, DateTime, Enum as SqlEnum, ForeignKey, Integer, JSON, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin


class UserRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    STAFF_ADMIN = "staff_admin"
    STAFF_OPERATOR = "staff_operator"
    CLIENT = "client"
    CLIENT_ADMIN = "client_admin"
    CLIENT_OPERATOR = "client_operator"
    BRANCH_MANAGER = "branch_manager"
    OPERATOR = "operator"


class ChannelMode(str, Enum):
    NORMAL = "normal"
    EXPANDED = "expanded"
    VIDEOWALL = "videowall"
    AUDIO = "audio"
    TOUCH = "touch"


class Orientation(str, Enum):
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


class ContentType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    URL = "url"
    HTML = "html"
    TEXT = "text"
    AUDIO = "audio"


class CampaignPlaybackMode(str, Enum):
    SEQUENTIAL = "sequential"
    RANDOM = "random"


class CampaignSequenceItemType(str, Enum):
    CONTENT = "content"
    LAYOUT = "layout"


class LayoutTemplate(str, Enum):
    SINGLE = "single"
    TWO_COLUMNS = "two_columns"
    FOUR_GRID = "four_grid"
    CUSTOM = "custom"


class ScheduleRecurrence(str, Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    CUSTOM = "custom"


class ChannelConnectionStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class KioskActionType(str, Enum):
    OPEN_URL = "open_url"
    SWITCH_SCREEN = "switch_screen"
    PLAY_VIDEO = "play_video"
    NAVIGATE_MENU = "navigate_menu"


class AudioPlaylistKind(str, Enum):
    MUSIC = "music"
    SPOT = "spot"


class AudioSpotRotationMode(str, Enum):
    SEQUENTIAL = "sequential"
    RANDOM = "random"


class AudioNormalizationStatus(str, Enum):
    PENDING = "pending"
    NORMALIZED = "normalized"
    SKIPPED = "skipped"


class AudioPlaybackEntryKind(str, Enum):
    MUSIC = "music"
    SPOT = "spot"


class DataSourceType(str, Enum):
    FILE_UPLOAD = "file_upload"
    GOOGLE_SHEETS = "google_sheets"
    API = "api"


class DataSourceStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"


class DatasetStatus(str, Enum):
    READY = "ready"
    PROCESSING = "processing"
    ERROR = "error"


class DatasetImportStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class DatasetColumnType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    EMPTY = "empty"


class LayoutBindingPreset(str, Enum):
    AUTOBUSES = "autobuses"
    AEROPUERTO = "aeropuerto"
    MENU = "menu"
    TURNERO = "turnero"


class LayoutRevisionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


def generate_channel_code() -> str:
    token = secrets.token_hex(4).upper()
    return f"TC-{token[:4]}-{token[4:]}"


class Client(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "clients"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    contact_email: Mapped[str | None] = mapped_column(String(160))
    brand_name: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), default="active")

    users: Mapped[list["User"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    branches: Mapped[list["Branch"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    channels: Mapped[list["Channel"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    campaigns: Mapped[list["Campaign"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    content_folders: Mapped[list["ContentFolder"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    contents: Mapped[list["ContentItem"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    layouts: Mapped[list["Layout"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    videowalls: Mapped[list["Videowall"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    kiosk_screens: Mapped[list["KioskScreen"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    touch_experiences: Mapped[list["TouchExperience"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    touch_assignments: Mapped[list["TouchExperienceAssignment"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    data_sources: Mapped[list["DataSource"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    datasets: Mapped[list["Dataset"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    audio_playlists: Mapped[list["AudioPlaylist"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    audio_assignments: Mapped[list["AudioAssignment"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    audio_playback_events: Mapped[list["AudioPlaybackEvent"]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
    )
    player_devices: Mapped[list["PlayerDevice"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    player_heartbeats: Mapped[list["PlayerHeartbeat"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    player_playback_events: Mapped[list["PlayerPlaybackEvent"]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
    )


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(SqlEnum(UserRole), nullable=False, default=UserRole.CLIENT)
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"))
    branch_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    client: Mapped[Client | None] = relationship(back_populates="users")
    branch: Mapped["Branch | None"] = relationship(back_populates="users")


class Branch(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("client_id", "code", name="uq_branch_client_code"),)

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    address: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(60), default="America/Mexico_City")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    client: Mapped[Client] = relationship(back_populates="branches")
    users: Mapped[list[User]] = relationship(back_populates="branch")
    channels: Mapped[list["Channel"]] = relationship(back_populates="branch", cascade="all, delete-orphan")
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="branch")
    touch_assignments: Mapped[list["TouchExperienceAssignment"]] = relationship(back_populates="branch")
    audio_assignments: Mapped[list["AudioAssignment"]] = relationship(back_populates="branch")
    audio_playback_events: Mapped[list["AudioPlaybackEvent"]] = relationship(back_populates="branch")
    player_devices: Mapped[list["PlayerDevice"]] = relationship(back_populates="branch")
    player_heartbeats: Mapped[list["PlayerHeartbeat"]] = relationship(back_populates="branch")
    player_playback_events: Mapped[list["PlayerPlaybackEvent"]] = relationship(back_populates="branch")


class Layout(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "layouts"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    template: Mapped[LayoutTemplate] = mapped_column(SqlEnum(LayoutTemplate), default=LayoutTemplate.SINGLE)
    canvas_width: Mapped[int] = mapped_column(Integer, default=1920)
    canvas_height: Mapped[int] = mapped_column(Integer, default=1080)
    zones: Mapped[list[dict]] = mapped_column(JSON, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    client: Mapped[Client] = relationship(back_populates="layouts")
    campaigns: Mapped[list["Campaign"]] = relationship(back_populates="layout")
    sequence_items: Mapped[list["CampaignSequenceItem"]] = relationship(back_populates="layout")
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="layout")
    data_bindings: Mapped[list["LayoutDataBinding"]] = relationship(back_populates="layout", cascade="all, delete-orphan")
    revisions: Mapped[list["LayoutRevision"]] = relationship(back_populates="layout", cascade="all, delete-orphan")


class Channel(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "channels"
    __table_args__ = (
        UniqueConstraint("client_id", "name", name="uq_channel_client_name"),
        UniqueConstraint("channel_code", name="uq_channel_code"),
    )

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    channel_code: Mapped[str] = mapped_column(String(32), nullable=False, default=generate_channel_code)
    resolution_width: Mapped[int] = mapped_column(Integer, default=1920)
    resolution_height: Mapped[int] = mapped_column(Integer, default=1080)
    orientation: Mapped[Orientation] = mapped_column(SqlEnum(Orientation), default=Orientation.HORIZONTAL)
    mode: Mapped[ChannelMode] = mapped_column(SqlEnum(ChannelMode), default=ChannelMode.NORMAL)
    screen_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[ChannelConnectionStatus] = mapped_column(
        SqlEnum(ChannelConnectionStatus),
        default=ChannelConnectionStatus.UNKNOWN,
    )
    current_playback: Mapped[str | None] = mapped_column(String(255))
    hardware_identifier: Mapped[str | None] = mapped_column(String(120))
    expanded_outputs: Mapped[list[dict]] = mapped_column(JSON, default=list)
    output_mapping_json: Mapped[dict] = mapped_column(JSON, default=dict)
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    client: Mapped[Client] = relationship(back_populates="channels")
    branch: Mapped[Branch] = relationship(back_populates="channels")
    assignments: Mapped[list["CampaignChannelAssignment"]] = relationship(
        back_populates="channel",
        cascade="all, delete-orphan",
    )
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="channel")
    videowall_nodes: Mapped[list["VideowallNode"]] = relationship(back_populates="channel")
    touch_assignments: Mapped[list["TouchExperienceAssignment"]] = relationship(back_populates="channel")
    audio_assignments: Mapped[list["AudioAssignment"]] = relationship(back_populates="channel")
    audio_playback_events: Mapped[list["AudioPlaybackEvent"]] = relationship(back_populates="channel")
    player_device: Mapped["PlayerDevice | None"] = relationship(back_populates="channel", uselist=False)
    player_heartbeats: Mapped[list["PlayerHeartbeat"]] = relationship(back_populates="channel")
    player_playback_events: Mapped[list["PlayerPlaybackEvent"]] = relationship(back_populates="channel")

    @property
    def last_heartbeat_at(self) -> datetime | None:
        return self.last_ping_at

    @property
    def heartbeat_age_seconds(self) -> int | None:
        from app.services.presence import heartbeat_age_seconds

        return heartbeat_age_seconds(self.last_heartbeat_at)

    @property
    def is_online(self) -> bool:
        from app.services.presence import is_player_online

        return is_player_online(self.last_heartbeat_at)

    @property
    def computed_status(self) -> str:
        from app.services.presence import resolve_presence_status

        return resolve_presence_status(self.last_heartbeat_at)


class Campaign(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "campaigns"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    layout_id: Mapped[str | None] = mapped_column(ForeignKey("layouts.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    default_duration_seconds: Mapped[int] = mapped_column(Integer, default=15)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    loop_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    playback_mode: Mapped[str] = mapped_column(String(32), default=CampaignPlaybackMode.SEQUENTIAL.value)

    client: Mapped[Client] = relationship(back_populates="campaigns")
    layout: Mapped[Layout | None] = relationship(back_populates="campaigns")
    playlist_items: Mapped[list["CampaignPlaylistItem"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
    )
    sequence_items: Mapped[list["CampaignSequenceItem"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
    )
    assignments: Mapped[list["CampaignChannelAssignment"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
    )
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="campaign", cascade="all, delete-orphan")


class ContentFolder(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "content_folders"
    __table_args__ = (UniqueConstraint("client_id", "parent_id", "name", name="uq_content_folder_parent_name"),)

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("content_folders.id"))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    client: Mapped[Client] = relationship(back_populates="content_folders")
    parent: Mapped["ContentFolder | None"] = relationship(remote_side="ContentFolder.id", back_populates="children")
    children: Mapped[list["ContentFolder"]] = relationship(back_populates="parent")
    contents: Mapped[list["ContentItem"]] = relationship(back_populates="folder")


class ContentItem(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "contents"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    folder_id: Mapped[str | None] = mapped_column(ForeignKey("content_folders.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    type: Mapped[ContentType] = mapped_column(SqlEnum(ContentType), nullable=False)
    file_path: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(255))
    html_content: Mapped[str | None] = mapped_column(Text)
    text_content: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=15)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    client: Mapped[Client] = relationship(back_populates="contents")
    folder: Mapped[ContentFolder | None] = relationship(back_populates="contents")
    playlist_items: Mapped[list["CampaignPlaylistItem"]] = relationship(back_populates="content")
    sequence_items: Mapped[list["CampaignSequenceItem"]] = relationship(back_populates="content")
    audio_playlist_items: Mapped[list["AudioPlaylistItem"]] = relationship(back_populates="content")
    audio_playback_events: Mapped[list["AudioPlaybackEvent"]] = relationship(back_populates="content")


class DataSource(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "data_sources"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[DataSourceType] = mapped_column(SqlEnum(DataSourceType), nullable=False)
    status: Mapped[DataSourceStatus] = mapped_column(SqlEnum(DataSourceStatus), default=DataSourceStatus.ACTIVE)
    description: Mapped[str | None] = mapped_column(Text)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    client: Mapped[Client] = relationship(back_populates="data_sources")
    datasets: Mapped[list["Dataset"]] = relationship(back_populates="data_source")


class Dataset(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("client_id", "slug", name="uq_dataset_client_slug"),)

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    data_source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[DatasetStatus] = mapped_column(SqlEnum(DatasetStatus), default=DatasetStatus.READY)
    current_import_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "dataset_imports.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_datasets_current_import",
        )
    )
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    column_count: Mapped[int] = mapped_column(Integer, default=0)

    client: Mapped[Client] = relationship(back_populates="datasets")
    data_source: Mapped[DataSource] = relationship(back_populates="datasets")
    imports: Mapped[list["DatasetImport"]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
        foreign_keys="DatasetImport.dataset_id",
    )
    current_import: Mapped["DatasetImport | None"] = relationship(
        foreign_keys=[current_import_id],
        post_update=True,
    )
    columns: Mapped[list["DatasetColumn"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")
    rows: Mapped[list["DatasetRow"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")
    layout_bindings: Mapped[list["LayoutDataBinding"]] = relationship(back_populates="dataset")


class DatasetImport(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "dataset_imports"

    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_mime_type: Mapped[str | None] = mapped_column(String(160))
    storage_path: Mapped[str] = mapped_column(String(255), nullable=False)
    import_status: Mapped[DatasetImportStatus] = mapped_column(
        SqlEnum(DatasetImportStatus),
        default=DatasetImportStatus.PENDING,
    )
    detected_sheet_name: Mapped[str | None] = mapped_column(String(160))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    column_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    summary_json: Mapped[dict] = mapped_column(JSON, default=dict)

    dataset: Mapped[Dataset] = relationship(back_populates="imports", foreign_keys=[dataset_id])
    columns: Mapped[list["DatasetColumn"]] = relationship(back_populates="import_record", cascade="all, delete-orphan")
    rows: Mapped[list["DatasetRow"]] = relationship(back_populates="import_record", cascade="all, delete-orphan")


class DatasetColumn(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "dataset_columns"
    __table_args__ = (UniqueConstraint("import_id", "column_key", name="uq_dataset_import_column_key"),)

    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    import_id: Mapped[str] = mapped_column(ForeignKey("dataset_imports.id", ondelete="CASCADE"), nullable=False)
    column_key: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    data_type: Mapped[DatasetColumnType] = mapped_column(SqlEnum(DatasetColumnType), default=DatasetColumnType.TEXT)
    position_index: Mapped[int] = mapped_column(Integer, default=0)
    sample_value: Mapped[str | None] = mapped_column(String(255))
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True)

    dataset: Mapped[Dataset] = relationship(back_populates="columns")
    import_record: Mapped[DatasetImport] = relationship(back_populates="columns")


class DatasetRow(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "dataset_rows"
    __table_args__ = (UniqueConstraint("import_id", "row_index", name="uq_dataset_import_row_index"),)

    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    import_id: Mapped[str] = mapped_column(ForeignKey("dataset_imports.id", ondelete="CASCADE"), nullable=False)
    row_index: Mapped[int] = mapped_column(Integer, default=0)
    row_data_json: Mapped[dict] = mapped_column(JSON, default=dict)
    row_hash: Mapped[str | None] = mapped_column(String(64))

    dataset: Mapped[Dataset] = relationship(back_populates="rows")
    import_record: Mapped[DatasetImport] = relationship(back_populates="rows")


class LayoutDataBinding(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "layout_data_bindings"

    layout_id: Mapped[str] = mapped_column(ForeignKey("layouts.id", ondelete="CASCADE"), nullable=False)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    preset_key: Mapped[LayoutBindingPreset] = mapped_column(SqlEnum(LayoutBindingPreset), nullable=False)
    zone_key: Mapped[str | None] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(Integer, default=1)
    max_rows: Mapped[int] = mapped_column(Integer, default=8)
    options_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    layout: Mapped[Layout] = relationship(back_populates="data_bindings")
    dataset: Mapped[Dataset] = relationship(back_populates="layout_bindings")
    fields: Mapped[list["LayoutBindingField"]] = relationship(back_populates="binding", cascade="all, delete-orphan")


class LayoutBindingField(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "layout_binding_fields"
    __table_args__ = (UniqueConstraint("binding_id", "target_field", name="uq_layout_binding_target_field"),)

    binding_id: Mapped[str] = mapped_column(ForeignKey("layout_data_bindings.id", ondelete="CASCADE"), nullable=False)
    target_field: Mapped[str] = mapped_column(String(80), nullable=False)
    column_key: Mapped[str | None] = mapped_column(String(120))
    display_label: Mapped[str | None] = mapped_column(String(120))
    fallback_value: Mapped[str | None] = mapped_column(String(255))
    format_hint: Mapped[str | None] = mapped_column(String(40))
    position_index: Mapped[int] = mapped_column(Integer, default=0)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    options_json: Mapped[dict] = mapped_column(JSON, default=dict)

    binding: Mapped[LayoutDataBinding] = relationship(back_populates="fields")


class LayoutRevision(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "layout_revisions"
    __table_args__ = (UniqueConstraint("layout_id", "revision_number", name="uq_layout_revision_number"),)

    layout_id: Mapped[str] = mapped_column(ForeignKey("layouts.id", ondelete="CASCADE"), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=LayoutRevisionStatus.DRAFT.value)
    notes: Mapped[str | None] = mapped_column(Text)
    editor_state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    preview_state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    is_current_draft: Mapped[bool] = mapped_column(Boolean, default=False)
    is_current_published: Mapped[bool] = mapped_column(Boolean, default=False)

    layout: Mapped[Layout] = relationship(back_populates="revisions")


class AudioPlaylist(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "audio_playlists"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[AudioPlaylistKind] = mapped_column(SqlEnum(AudioPlaylistKind), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    client: Mapped[Client] = relationship(back_populates="audio_playlists")
    items: Mapped[list["AudioPlaylistItem"]] = relationship(
        back_populates="playlist",
        cascade="all, delete-orphan",
    )
    music_assignments: Mapped[list["AudioAssignment"]] = relationship(
        back_populates="music_playlist",
        foreign_keys="AudioAssignment.music_playlist_id",
    )
    spot_assignments: Mapped[list["AudioAssignment"]] = relationship(
        back_populates="spot_playlist",
        foreign_keys="AudioAssignment.spot_playlist_id",
    )
    playback_events: Mapped[list["AudioPlaybackEvent"]] = relationship(back_populates="playlist")


class AudioPlaylistItem(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "audio_playlist_items"
    __table_args__ = (UniqueConstraint("playlist_id", "sort_order", name="uq_audio_playlist_sort_order"),)

    playlist_id: Mapped[str] = mapped_column(ForeignKey("audio_playlists.id", ondelete="CASCADE"), nullable=False)
    content_id: Mapped[str] = mapped_column(ForeignKey("contents.id", ondelete="CASCADE"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=1)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    playlist: Mapped["AudioPlaylist"] = relationship(back_populates="items")
    content: Mapped[ContentItem] = relationship(back_populates="audio_playlist_items")


class AudioAssignment(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "audio_assignments"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    branch_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"))
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), unique=True)
    music_playlist_id: Mapped[str | None] = mapped_column(ForeignKey("audio_playlists.id", ondelete="SET NULL"))
    spot_playlist_id: Mapped[str | None] = mapped_column(ForeignKey("audio_playlists.id", ondelete="SET NULL"))
    songs_between_spots: Mapped[int] = mapped_column(Integer, default=3)
    spots_per_break: Mapped[int] = mapped_column(Integer, default=1)
    spot_rotation_mode: Mapped[AudioSpotRotationMode] = mapped_column(
        SqlEnum(AudioSpotRotationMode),
        default=AudioSpotRotationMode.SEQUENTIAL,
    )
    avoid_consecutive_spots: Mapped[bool] = mapped_column(Boolean, default=True)
    volume_normalization_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    volume_normalization_status: Mapped[AudioNormalizationStatus] = mapped_column(
        SqlEnum(AudioNormalizationStatus),
        default=AudioNormalizationStatus.PENDING,
    )
    target_lufs: Mapped[int] = mapped_column(Integer, default=-14)

    client: Mapped[Client] = relationship(back_populates="audio_assignments")
    branch: Mapped[Branch | None] = relationship(back_populates="audio_assignments")
    channel: Mapped[Channel | None] = relationship(back_populates="audio_assignments")
    music_playlist: Mapped[AudioPlaylist | None] = relationship(
        back_populates="music_assignments",
        foreign_keys=[music_playlist_id],
    )
    spot_playlist: Mapped[AudioPlaylist | None] = relationship(
        back_populates="spot_assignments",
        foreign_keys=[spot_playlist_id],
    )


class AudioPlaybackEvent(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "audio_playback_events"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    branch_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    playlist_id: Mapped[str | None] = mapped_column(ForeignKey("audio_playlists.id", ondelete="SET NULL"))
    content_id: Mapped[str | None] = mapped_column(ForeignKey("contents.id", ondelete="SET NULL"))
    entry_kind: Mapped[AudioPlaybackEntryKind] = mapped_column(SqlEnum(AudioPlaybackEntryKind), nullable=False)
    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    client: Mapped[Client] = relationship(back_populates="audio_playback_events")
    branch: Mapped[Branch | None] = relationship(back_populates="audio_playback_events")
    channel: Mapped[Channel | None] = relationship(back_populates="audio_playback_events")
    playlist: Mapped[AudioPlaylist | None] = relationship(back_populates="playback_events")
    content: Mapped[ContentItem | None] = relationship(back_populates="audio_playback_events")


class CampaignPlaylistItem(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "campaign_playlist_items"
    __table_args__ = (UniqueConstraint("campaign_id", "sort_order", name="uq_campaign_sort_order"),)

    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    content_id: Mapped[str] = mapped_column(ForeignKey("contents.id", ondelete="CASCADE"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=1)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=15)
    zone_key: Mapped[str] = mapped_column(String(80), default="main")

    campaign: Mapped[Campaign] = relationship(back_populates="playlist_items")
    content: Mapped[ContentItem] = relationship(back_populates="playlist_items")


class CampaignSequenceItem(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "campaign_sequence_items"
    __table_args__ = (UniqueConstraint("campaign_id", "sort_order", name="uq_campaign_sequence_sort_order"),)

    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    item_type: Mapped[CampaignSequenceItemType] = mapped_column(
        SqlEnum(
            CampaignSequenceItemType,
            native_enum=False,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=CampaignSequenceItemType.CONTENT,
    )
    content_id: Mapped[str | None] = mapped_column(ForeignKey("contents.id", ondelete="CASCADE"))
    layout_id: Mapped[str | None] = mapped_column(ForeignKey("layouts.id", ondelete="CASCADE"))
    sort_order: Mapped[int] = mapped_column(Integer, default=1)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=15)
    options_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    campaign: Mapped[Campaign] = relationship(back_populates="sequence_items")
    content: Mapped[ContentItem | None] = relationship(back_populates="sequence_items")
    layout: Mapped[Layout | None] = relationship(back_populates="sequence_items")


class CampaignChannelAssignment(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "campaign_channel_assignments"
    __table_args__ = (UniqueConstraint("campaign_id", "channel_id", name="uq_campaign_channel"),)

    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=1)
    active_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    campaign: Mapped[Campaign] = relationship(back_populates="assignments")
    channel: Mapped[Channel] = relationship(back_populates="assignments")


class Schedule(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "schedules"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    branch_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))
    layout_id: Mapped[str | None] = mapped_column(ForeignKey("layouts.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    recurrence: Mapped[ScheduleRecurrence] = mapped_column(SqlEnum(ScheduleRecurrence), default=ScheduleRecurrence.DAILY)
    days_of_week: Mapped[list[int]] = mapped_column(JSON, default=list)
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_looping: Mapped[bool] = mapped_column(Boolean, default=True)
    timezone: Mapped[str] = mapped_column(String(60), default="America/Mexico_City")
    priority: Mapped[int] = mapped_column(Integer, default=100)

    client: Mapped[Client] = relationship(back_populates="schedules")
    campaign: Mapped[Campaign] = relationship(back_populates="schedules")
    channel: Mapped[Channel | None] = relationship(back_populates="schedules")
    branch: Mapped[Branch | None] = relationship(back_populates="schedules")
    layout: Mapped[Layout | None] = relationship(back_populates="schedules")


class Videowall(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "videowalls"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    columns: Mapped[int] = mapped_column(Integer, default=2)
    rows: Mapped[int] = mapped_column(Integer, default=2)
    total_width: Mapped[int] = mapped_column(Integer, default=3840)
    total_height: Mapped[int] = mapped_column(Integer, default=2160)
    start_tolerance_ms: Mapped[int] = mapped_column(Integer, default=250)
    sync_mode: Mapped[str] = mapped_column(String(40), default="play_at_timestamp")

    client: Mapped[Client] = relationship(back_populates="videowalls")
    nodes: Mapped[list["VideowallNode"]] = relationship(back_populates="videowall", cascade="all, delete-orphan")


class VideowallNode(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "videowall_nodes"
    __table_args__ = (UniqueConstraint("videowall_id", "channel_id", name="uq_videowall_channel"),)

    videowall_id: Mapped[str] = mapped_column(ForeignKey("videowalls.id", ondelete="CASCADE"), nullable=False)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    position_index: Mapped[int] = mapped_column(Integer, default=1)
    row_index: Mapped[int] = mapped_column(Integer, default=0)
    column_index: Mapped[int] = mapped_column(Integer, default=0)
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=1920)
    height: Mapped[int] = mapped_column(Integer, default=1080)

    videowall: Mapped[Videowall] = relationship(back_populates="nodes")
    channel: Mapped[Channel] = relationship(back_populates="videowall_nodes")


class KioskScreen(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "kiosk_screens"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    experience_id: Mapped[str | None] = mapped_column(ForeignKey("touch_experiences.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    background_url: Mapped[str | None] = mapped_column(String(255))
    attract_media_url: Mapped[str | None] = mapped_column(String(255))
    inactivity_timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    is_attract_screen: Mapped[bool] = mapped_column(Boolean, default=False)
    screen_kind: Mapped[str] = mapped_column(String(40), default="custom")
    sort_order: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    idle_timeout_override: Mapped[int | None] = mapped_column(Integer)

    client: Mapped[Client] = relationship(back_populates="kiosk_screens")
    experience: Mapped["TouchExperience | None"] = relationship(
        back_populates="screens",
        foreign_keys=[experience_id],
    )
    buttons: Mapped[list["KioskButton"]] = relationship(
        back_populates="screen",
        cascade="all, delete-orphan",
        foreign_keys="KioskButton.screen_id",
    )


class KioskButton(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "kiosk_buttons"

    screen_id: Mapped[str] = mapped_column(ForeignKey("kiosk_screens.id", ondelete="CASCADE"), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=240)
    height: Mapped[int] = mapped_column(Integer, default=72)
    action_type: Mapped[KioskActionType] = mapped_column(SqlEnum(KioskActionType), nullable=False)
    action_value: Mapped[str | None] = mapped_column(String(255))
    target_screen_id: Mapped[str | None] = mapped_column(ForeignKey("kiosk_screens.id", ondelete="SET NULL"))
    sort_order: Mapped[int] = mapped_column(Integer, default=1)
    style_json: Mapped[dict] = mapped_column(JSON, default=dict)
    action_payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_hotspot: Mapped[bool] = mapped_column(Boolean, default=False)

    screen: Mapped[KioskScreen] = relationship(
        back_populates="buttons",
        foreign_keys=[screen_id],
    )


class TouchExperience(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "touch_experiences"
    __table_args__ = (UniqueConstraint("client_id", "slug", name="uq_touch_experience_client_slug"),)

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    attract_screen_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "kiosk_screens.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_touch_experiences_attract_screen",
        )
    )
    home_screen_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "kiosk_screens.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_touch_experiences_home_screen",
        )
    )
    default_idle_timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    client: Mapped[Client] = relationship(back_populates="touch_experiences")
    screens: Mapped[list[KioskScreen]] = relationship(
        back_populates="experience",
        foreign_keys="KioskScreen.experience_id",
    )
    locations: Mapped[list["TouchLocation"]] = relationship(back_populates="experience", cascade="all, delete-orphan")
    maps: Mapped[list["TouchMap"]] = relationship(back_populates="experience", cascade="all, delete-orphan")
    assignments: Mapped[list["TouchExperienceAssignment"]] = relationship(back_populates="experience", cascade="all, delete-orphan")
    attract_screen: Mapped[KioskScreen | None] = relationship(foreign_keys=[attract_screen_id], post_update=True)
    home_screen: Mapped[KioskScreen | None] = relationship(foreign_keys=[home_screen_id], post_update=True)


class TouchExperienceAssignment(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "touch_experience_assignments"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    experience_id: Mapped[str] = mapped_column(ForeignKey("touch_experiences.id", ondelete="CASCADE"), nullable=False)
    branch_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"))
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    sort_order: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    client: Mapped[Client] = relationship(back_populates="touch_assignments")
    experience: Mapped[TouchExperience] = relationship(back_populates="assignments")
    branch: Mapped[Branch | None] = relationship(back_populates="touch_assignments")
    channel: Mapped[Channel | None] = relationship(back_populates="touch_assignments")


class PlayerDevice(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "player_devices"

    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, unique=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False)
    hardware_id: Mapped[str] = mapped_column(String(160), nullable=False)
    device_name: Mapped[str] = mapped_column(String(160), nullable=False)
    app_version: Mapped[str | None] = mapped_column(String(64))
    player_token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    channel: Mapped[Channel] = relationship(back_populates="player_device")
    client: Mapped[Client] = relationship(back_populates="player_devices")
    branch: Mapped[Branch] = relationship(back_populates="player_devices")
    heartbeats: Mapped[list["PlayerHeartbeat"]] = relationship(back_populates="player_device", cascade="all, delete-orphan")
    playback_events: Mapped[list["PlayerPlaybackEvent"]] = relationship(
        back_populates="player_device",
        cascade="all, delete-orphan",
    )


class PlayerHeartbeat(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "player_heartbeats"

    player_device_id: Mapped[str] = mapped_column(ForeignKey("player_devices.id", ondelete="CASCADE"), nullable=False)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="online")
    current_content_id: Mapped[str | None] = mapped_column(ForeignKey("contents.id", ondelete="SET NULL"))
    current_campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"))
    current_layout_id: Mapped[str | None] = mapped_column(ForeignKey("layouts.id", ondelete="SET NULL"))
    resolution_json: Mapped[dict] = mapped_column(JSON, default=dict)
    mode: Mapped[str | None] = mapped_column(String(40))
    app_version: Mapped[str | None] = mapped_column(String(64))
    cache_status_json: Mapped[dict] = mapped_column(JSON, default=dict)
    errors_json: Mapped[list] = mapped_column(JSON, default=list)
    local_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    playback_position: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    player_device: Mapped[PlayerDevice] = relationship(back_populates="heartbeats")
    channel: Mapped[Channel] = relationship(back_populates="player_heartbeats")
    client: Mapped[Client] = relationship(back_populates="player_heartbeats")
    branch: Mapped[Branch] = relationship(back_populates="player_heartbeats")
    current_content: Mapped[ContentItem | None] = relationship(foreign_keys=[current_content_id])
    current_campaign: Mapped[Campaign | None] = relationship(foreign_keys=[current_campaign_id])
    current_layout: Mapped[Layout | None] = relationship(foreign_keys=[current_layout_id])


class PlayerPlaybackEvent(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "player_playback_events"

    player_device_id: Mapped[str] = mapped_column(ForeignKey("player_devices.id", ondelete="CASCADE"), nullable=False)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False)
    content_id: Mapped[str | None] = mapped_column(ForeignKey("contents.id", ondelete="SET NULL"))
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"))
    layout_id: Mapped[str | None] = mapped_column(ForeignKey("layouts.id", ondelete="SET NULL"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), default="completed")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    player_device: Mapped[PlayerDevice] = relationship(back_populates="playback_events")
    channel: Mapped[Channel] = relationship(back_populates="player_playback_events")
    client: Mapped[Client] = relationship(back_populates="player_playback_events")
    branch: Mapped[Branch] = relationship(back_populates="player_playback_events")
    content: Mapped[ContentItem | None] = relationship()
    campaign: Mapped[Campaign | None] = relationship()
    layout: Mapped[Layout | None] = relationship()


class TouchLocation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "touch_locations"

    experience_id: Mapped[str] = mapped_column(ForeignKey("touch_experiences.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    floor_zone: Mapped[str | None] = mapped_column(String(80))
    suite: Mapped[str | None] = mapped_column(String(60))
    image_url: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    experience: Mapped[TouchExperience] = relationship(back_populates="locations")


class TouchMap(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "touch_maps"

    experience_id: Mapped[str] = mapped_column(ForeignKey("touch_experiences.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    floor_zone: Mapped[str | None] = mapped_column(String(80))
    background_url: Mapped[str | None] = mapped_column(String(255))
    overlay_url: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    experience: Mapped[TouchExperience] = relationship(back_populates="maps")
