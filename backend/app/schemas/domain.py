from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, EmailStr, Field

from app.models.entities import (
    AudioNormalizationStatus,
    AudioPlaybackEntryKind,
    AudioPlaylistKind,
    AudioSpotRotationMode,
    ChannelConnectionStatus,
    ChannelMode,
    ContentType,
    DataSourceStatus,
    DataSourceType,
    DatasetColumnType,
    DatasetImportStatus,
    DatasetStatus,
    KioskActionType,
    LayoutBindingPreset,
    LayoutRevisionStatus,
    LayoutTemplate,
    Orientation,
    ScheduleRecurrence,
    UserRole,
)
from app.schemas.common import TimestampedModel


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str
    password: str
    role: UserRole = UserRole.CLIENT_ADMIN
    client_id: str | None = None
    branch_id: str | None = None
    status: Literal["active", "suspended"] = "active"


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = None
    password: str | None = None
    role: UserRole | None = None
    branch_id: str | None = None


class UserStatusUpdate(BaseModel):
    status: Literal["active", "suspended"]


class UserRead(TimestampedModel):
    email: EmailStr
    full_name: str
    role: UserRole
    client_id: str | None = None
    branch_id: str | None = None
    is_active: bool
    status: Literal["active", "suspended"]
    last_login_at: datetime | None = None


class ClientCreate(BaseModel):
    name: str
    slug: str
    contact_email: EmailStr | None = None
    brand_name: str | None = None


class ClientUpdate(BaseModel):
    name: str | None = None
    slug: str | None = None
    contact_email: EmailStr | None = None
    brand_name: str | None = None


class ClientStatusUpdate(BaseModel):
    status: Literal["active", "suspended"]


class ClientDeleteRequest(BaseModel):
    confirm_text: str


class ClientRead(TimestampedModel):
    name: str
    slug: str
    contact_email: EmailStr | None = None
    brand_name: str | None = None
    status: str


class BranchCreate(BaseModel):
    client_id: str | None = None
    name: str
    code: str
    address: str | None = None
    timezone: str = "America/Mexico_City"


class BranchUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    address: str | None = None
    timezone: str | None = None


class BranchRead(TimestampedModel):
    client_id: str
    name: str
    code: str
    address: str | None = None
    timezone: str
    is_active: bool


class LayoutCreate(BaseModel):
    client_id: str | None = None
    name: str
    template: LayoutTemplate = LayoutTemplate.SINGLE
    canvas_width: int = 1920
    canvas_height: int = 1080
    zones: list[dict] = Field(default_factory=list)
    is_default: bool = False


class LayoutRead(TimestampedModel):
    client_id: str
    name: str
    template: LayoutTemplate
    canvas_width: int
    canvas_height: int
    zones: list[dict]
    is_default: bool


class LayoutRevisionCreate(BaseModel):
    name: str | None = None
    notes: str | None = None
    clone_from_revision_id: str | None = None


class LayoutRevisionUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None
    editor_state_json: dict | None = None


class LayoutRevisionRead(TimestampedModel):
    layout_id: str
    revision_number: int
    name: str
    status: LayoutRevisionStatus
    notes: str | None = None
    editor_state_json: dict
    preview_state_json: dict
    published_at: datetime | None = None
    created_by_user_id: str | None = None
    is_current_draft: bool
    is_current_published: bool


class LayoutDataBindingCreate(BaseModel):
    dataset_id: str
    name: str
    preset_key: LayoutBindingPreset
    zone_key: str | None = None
    sort_order: int = 1
    max_rows: int = 8
    options_json: dict = Field(default_factory=dict)
    is_active: bool = True


class LayoutDataBindingUpdate(BaseModel):
    dataset_id: str | None = None
    name: str | None = None
    preset_key: LayoutBindingPreset | None = None
    zone_key: str | None = None
    sort_order: int | None = None
    max_rows: int | None = None
    options_json: dict | None = None
    is_active: bool | None = None


class LayoutDataBindingRead(TimestampedModel):
    layout_id: str
    dataset_id: str
    name: str
    preset_key: LayoutBindingPreset
    zone_key: str | None = None
    sort_order: int
    max_rows: int
    options_json: dict
    is_active: bool


class LayoutBindingFieldCreate(BaseModel):
    target_field: str
    column_key: str | None = None
    display_label: str | None = None
    fallback_value: str | None = None
    format_hint: str | None = None
    position_index: int = 0
    is_required: bool = True
    options_json: dict = Field(default_factory=dict)


class LayoutBindingFieldSet(BaseModel):
    fields: list[LayoutBindingFieldCreate] = Field(default_factory=list)


class LayoutBindingFieldRead(TimestampedModel):
    binding_id: str
    target_field: str
    column_key: str | None = None
    display_label: str | None = None
    fallback_value: str | None = None
    format_hint: str | None = None
    position_index: int
    is_required: bool
    options_json: dict


class LayoutBindingValidationRequest(BaseModel):
    dataset_id: str
    preset_key: LayoutBindingPreset
    zone_key: str | None = None
    max_rows: int = 8
    options_json: dict = Field(default_factory=dict)
    fields: list[LayoutBindingFieldCreate] = Field(default_factory=list)


class ChannelCreate(BaseModel):
    client_id: str | None = None
    branch_id: str
    name: str
    resolution_width: int = 1920
    resolution_height: int = 1080
    orientation: Orientation = Orientation.HORIZONTAL
    mode: ChannelMode = ChannelMode.NORMAL
    screen_count: int = 1
    hardware_identifier: str | None = None
    expanded_outputs: list[dict] = Field(default_factory=list)
    output_mapping_json: dict = Field(default_factory=dict)
    notes: str | None = None


class ChannelUpdate(BaseModel):
    name: str | None = None
    resolution_width: int | None = None
    resolution_height: int | None = None
    orientation: Orientation | None = None
    screen_count: int | None = None
    hardware_identifier: str | None = None
    expanded_outputs: list[dict] | None = None
    output_mapping_json: dict | None = None
    notes: str | None = None


class ChannelRead(TimestampedModel):
    client_id: str
    branch_id: str
    name: str
    channel_code: str
    resolution_width: int
    resolution_height: int
    orientation: Orientation
    mode: ChannelMode
    screen_count: int
    status: ChannelConnectionStatus = Field(validation_alias=AliasChoices("computed_status", "status"))
    current_playback: str | None = None
    hardware_identifier: str | None = None
    expanded_outputs: list[dict]
    output_mapping_json: dict
    last_ping_at: datetime | None = Field(default=None, validation_alias=AliasChoices("last_heartbeat_at", "last_ping_at"))
    last_heartbeat_at: datetime | None = Field(default=None, validation_alias=AliasChoices("last_heartbeat_at", "last_ping_at"))
    heartbeat_age_seconds: int | None = None
    is_online: bool = False
    notes: str | None = None


class CampaignCreate(BaseModel):
    client_id: str | None = None
    layout_id: str | None = None
    name: str
    description: str | None = None
    default_duration_seconds: int = 15
    is_active: bool = True
    loop_enabled: bool = True
    playback_mode: Literal["sequential", "random"] = "sequential"


class CampaignRead(TimestampedModel):
    client_id: str
    layout_id: str | None = None
    name: str
    description: str | None = None
    default_duration_seconds: int
    is_active: bool
    loop_enabled: bool
    playback_mode: Literal["sequential", "random"]


class ContentCreate(BaseModel):
    client_id: str | None = None
    folder_id: str | None = None
    name: str
    type: ContentType
    file_path: str | None = None
    source_url: str | None = None
    html_content: str | None = None
    text_content: str | None = None
    duration_seconds: int = 15
    metadata_json: dict = Field(default_factory=dict)


class ContentRead(TimestampedModel):
    client_id: str
    folder_id: str | None = None
    name: str
    type: ContentType
    file_path: str | None = None
    source_url: str | None = None
    html_content: str | None = None
    text_content: str | None = None
    duration_seconds: int
    metadata_json: dict


class ContentFolderCreate(BaseModel):
    client_id: str | None = None
    parent_id: str | None = None
    name: str
    sort_order: int = 0


class ContentFolderUpdate(BaseModel):
    name: str


class ContentFolderRead(TimestampedModel):
    client_id: str
    parent_id: str | None = None
    name: str
    sort_order: int
    is_active: bool


class ContentFolderMove(BaseModel):
    folder_id: str | None = None


class ContentFolderDeleteImpactRead(BaseModel):
    folder_id: str
    folder_name: str
    folder_count: int
    subfolder_count: int
    content_count: int
    referenced_campaign_count: int
    published_campaign_count: int
    published_campaign_names: list[str] = Field(default_factory=list)
    has_published_content: bool


class DataSourceCreate(BaseModel):
    client_id: str | None = None
    name: str
    source_type: DataSourceType = DataSourceType.FILE_UPLOAD
    description: str | None = None
    config_json: dict = Field(default_factory=dict)
    is_active: bool = True


class DataSourceRead(TimestampedModel):
    client_id: str
    name: str
    source_type: DataSourceType
    status: DataSourceStatus
    description: str | None = None
    config_json: dict
    is_active: bool


class DatasetCreate(BaseModel):
    client_id: str | None = None
    data_source_id: str
    name: str
    slug: str
    description: str | None = None


class DatasetRead(TimestampedModel):
    client_id: str
    data_source_id: str
    name: str
    slug: str
    description: str | None = None
    status: DatasetStatus
    current_import_id: str | None = None
    row_count: int
    column_count: int


class DatasetImportRead(TimestampedModel):
    dataset_id: str
    source_filename: str
    source_mime_type: str | None = None
    storage_path: str
    import_status: DatasetImportStatus
    detected_sheet_name: str | None = None
    row_count: int
    column_count: int
    imported_at: datetime
    summary_json: dict


class DatasetColumnRead(TimestampedModel):
    dataset_id: str
    import_id: str
    column_key: str
    display_name: str
    source_name: str
    data_type: DatasetColumnType
    position_index: int
    sample_value: str | None = None
    is_visible: bool


class DatasetRowRead(TimestampedModel):
    dataset_id: str
    import_id: str
    row_index: int
    row_data_json: dict
    row_hash: str | None = None


class AudioPlaylistCreate(BaseModel):
    client_id: str | None = None
    name: str
    kind: AudioPlaylistKind
    description: str | None = None
    is_active: bool = True


class AudioPlaylistRead(TimestampedModel):
    client_id: str
    name: str
    kind: AudioPlaylistKind
    description: str | None = None
    is_active: bool


class AudioPlaylistItemCreate(BaseModel):
    content_id: str
    sort_order: int = 1
    is_enabled: bool = True


class AudioPlaylistItemRead(TimestampedModel):
    playlist_id: str
    content_id: str
    sort_order: int
    is_enabled: bool


class AudioAssignmentCreate(BaseModel):
    client_id: str | None = None
    branch_id: str | None = None
    channel_id: str | None = None
    music_playlist_id: str | None = None
    spot_playlist_id: str | None = None
    songs_between_spots: int = 3
    spots_per_break: int = 1
    spot_rotation_mode: AudioSpotRotationMode = AudioSpotRotationMode.SEQUENTIAL
    avoid_consecutive_spots: bool = True
    volume_normalization_enabled: bool = False
    volume_normalization_status: AudioNormalizationStatus = AudioNormalizationStatus.PENDING
    target_lufs: int = -14


class AudioAssignmentRead(TimestampedModel):
    client_id: str
    branch_id: str | None = None
    channel_id: str | None = None
    music_playlist_id: str | None = None
    spot_playlist_id: str | None = None
    songs_between_spots: int
    spots_per_break: int
    spot_rotation_mode: AudioSpotRotationMode
    avoid_consecutive_spots: bool
    volume_normalization_enabled: bool
    volume_normalization_status: AudioNormalizationStatus
    target_lufs: int


class AudioPlaybackEventCreate(BaseModel):
    client_id: str | None = None
    branch_id: str | None = None
    channel_id: str | None = None
    playlist_id: str | None = None
    content_id: str | None = None
    entry_kind: AudioPlaybackEntryKind
    played_at: datetime | None = None


class AudioPlaybackEventRead(TimestampedModel):
    client_id: str
    branch_id: str | None = None
    channel_id: str | None = None
    playlist_id: str | None = None
    content_id: str | None = None
    entry_kind: AudioPlaybackEntryKind
    played_at: datetime


class CampaignPlaylistItemCreate(BaseModel):
    content_id: str
    sort_order: int = 1
    duration_seconds: int = 15
    zone_key: str = "main"


class CampaignPlaylistItemUpdate(BaseModel):
    duration_seconds: int | None = None
    sort_order: int | None = None
    zone_key: str | None = None


class CampaignPlaylistReorderRequest(BaseModel):
    ordered_item_ids: list[str] = Field(default_factory=list)


class CampaignPlaylistItemRead(TimestampedModel):
    campaign_id: str
    content_id: str
    sort_order: int
    duration_seconds: int
    zone_key: str


class CampaignSequenceItemCreate(BaseModel):
    item_type: Literal["content", "layout"] = "content"
    content_id: str | None = None
    layout_id: str | None = None
    sort_order: int = 1
    duration_seconds: int = 15
    options_json: dict = Field(default_factory=dict)
    is_enabled: bool = True


class CampaignSequenceItemUpdate(BaseModel):
    duration_seconds: int | None = None
    sort_order: int | None = None
    options_json: dict | None = None
    is_enabled: bool | None = None


class CampaignSequenceReorderRequest(BaseModel):
    ordered_item_ids: list[str] = Field(default_factory=list)


class CampaignSequenceItemRead(TimestampedModel):
    campaign_id: str
    item_type: Literal["content", "layout"]
    content_id: str | None = None
    layout_id: str | None = None
    sort_order: int
    duration_seconds: int
    options_json: dict
    is_enabled: bool


class CampaignPlaybackModeUpdate(BaseModel):
    playback_mode: Literal["sequential", "random"]


class CampaignAssignmentCreate(BaseModel):
    channel_id: str
    priority: int = 1


class ChannelCampaignAssignmentReplaceRequest(BaseModel):
    campaign_id: str
    priority: int = 1


class CampaignAssignmentResult(BaseModel):
    assignment_id: str
    campaign_id: str
    channel_id: str
    assignment_status: Literal["created", "existing", "replaced"]
    detail: str


class ScheduleCreate(BaseModel):
    client_id: str | None = None
    campaign_id: str
    channel_id: str | None = None
    branch_id: str | None = None
    layout_id: str | None = None
    title: str
    recurrence: ScheduleRecurrence = ScheduleRecurrence.DAILY
    days_of_week: list[int] = Field(default_factory=list)
    starts_on: date | None = None
    ends_on: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    is_active: bool = True
    is_looping: bool = True
    timezone: str = "America/Mexico_City"
    priority: int = 100


class ScheduleUpdate(BaseModel):
    campaign_id: str | None = None
    layout_id: str | None = None
    title: str | None = None
    recurrence: ScheduleRecurrence | None = None
    days_of_week: list[int] | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    is_active: bool | None = None
    is_looping: bool | None = None
    timezone: str | None = None
    priority: int | None = None


class ScheduleRead(TimestampedModel):
    client_id: str
    campaign_id: str
    channel_id: str | None = None
    branch_id: str | None = None
    layout_id: str | None = None
    title: str
    recurrence: ScheduleRecurrence
    days_of_week: list[int]
    starts_on: date | None = None
    ends_on: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    is_active: bool
    is_looping: bool
    timezone: str
    priority: int


class VideowallCreate(BaseModel):
    client_id: str | None = None
    name: str
    columns: int = 2
    rows: int = 2
    total_width: int = 3840
    total_height: int = 2160
    start_tolerance_ms: int = 250
    sync_mode: str = "play_at_timestamp"


class VideowallUpdate(BaseModel):
    name: str | None = None
    columns: int | None = None
    rows: int | None = None
    total_width: int | None = None
    total_height: int | None = None
    start_tolerance_ms: int | None = None
    sync_mode: str | None = None


class VideowallRead(TimestampedModel):
    client_id: str
    name: str
    columns: int
    rows: int
    total_width: int
    total_height: int
    start_tolerance_ms: int
    sync_mode: str


class VideowallNodeCreate(BaseModel):
    channel_id: str
    position_index: int
    row_index: int
    column_index: int
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None


class VideowallNodeUpdate(BaseModel):
    position_index: int | None = None
    row_index: int | None = None
    column_index: int | None = None


class VideowallNodeRead(TimestampedModel):
    videowall_id: str
    channel_id: str
    position_index: int
    row_index: int
    column_index: int
    x: int
    y: int
    width: int
    height: int


class KioskScreenCreate(BaseModel):
    client_id: str | None = None
    experience_id: str | None = None
    name: str
    slug: str
    background_url: str | None = None
    attract_media_url: str | None = None
    inactivity_timeout_seconds: int = 30
    is_attract_screen: bool = False
    screen_kind: str = "custom"
    sort_order: int = 1
    metadata_json: dict = Field(default_factory=dict)
    idle_timeout_override: int | None = None


class KioskScreenRead(TimestampedModel):
    client_id: str
    experience_id: str | None = None
    name: str
    slug: str
    background_url: str | None = None
    attract_media_url: str | None = None
    inactivity_timeout_seconds: int
    is_attract_screen: bool
    screen_kind: str
    sort_order: int
    metadata_json: dict
    idle_timeout_override: int | None = None


class KioskButtonCreate(BaseModel):
    label: str
    x: int
    y: int
    width: int
    height: int
    action_type: KioskActionType
    action_value: str | None = None
    target_screen_id: str | None = None
    sort_order: int = 1
    style_json: dict = Field(default_factory=dict)
    action_payload_json: dict = Field(default_factory=dict)
    is_hotspot: bool = False


class KioskButtonRead(TimestampedModel):
    screen_id: str
    label: str
    x: int
    y: int
    width: int
    height: int
    action_type: KioskActionType
    action_value: str | None = None
    target_screen_id: str | None = None
    sort_order: int
    style_json: dict
    action_payload_json: dict
    is_hotspot: bool


class TouchExperienceCreate(BaseModel):
    client_id: str | None = None
    name: str
    slug: str
    description: str | None = None
    attract_screen_id: str | None = None
    home_screen_id: str | None = None
    default_idle_timeout_seconds: int = 30
    is_active: bool = True
    metadata_json: dict = Field(default_factory=dict)


class TouchExperienceUpdate(BaseModel):
    name: str | None = None
    slug: str | None = None
    description: str | None = None
    attract_screen_id: str | None = None
    home_screen_id: str | None = None
    default_idle_timeout_seconds: int | None = None
    is_active: bool | None = None
    metadata_json: dict | None = None


class TouchExperienceRead(TimestampedModel):
    client_id: str
    name: str
    slug: str
    description: str | None = None
    attract_screen_id: str | None = None
    home_screen_id: str | None = None
    default_idle_timeout_seconds: int
    is_active: bool
    metadata_json: dict


class TouchExperienceAssignmentCreate(BaseModel):
    branch_id: str | None = None
    channel_id: str | None = None
    sort_order: int = 1
    is_active: bool = True
    metadata_json: dict = Field(default_factory=dict)


class TouchExperienceAssignmentUpdate(BaseModel):
    branch_id: str | None = None
    channel_id: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    metadata_json: dict | None = None


class TouchExperienceAssignmentRead(TimestampedModel):
    client_id: str
    experience_id: str
    branch_id: str | None = None
    channel_id: str | None = None
    sort_order: int
    is_active: bool
    metadata_json: dict


class TouchLocationCreate(BaseModel):
    name: str
    category: str | None = None
    description: str | None = None
    floor_zone: str | None = None
    suite: str | None = None
    image_url: str | None = None
    metadata_json: dict = Field(default_factory=dict)
    is_active: bool = True


class TouchLocationUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    description: str | None = None
    floor_zone: str | None = None
    suite: str | None = None
    image_url: str | None = None
    metadata_json: dict | None = None
    is_active: bool | None = None


class TouchLocationRead(TimestampedModel):
    experience_id: str
    name: str
    category: str | None = None
    description: str | None = None
    floor_zone: str | None = None
    suite: str | None = None
    image_url: str | None = None
    metadata_json: dict
    is_active: bool


class TouchMapCreate(BaseModel):
    name: str
    floor_zone: str | None = None
    background_url: str | None = None
    overlay_url: str | None = None
    metadata_json: dict = Field(default_factory=dict)
    is_active: bool = True


class TouchMapUpdate(BaseModel):
    name: str | None = None
    floor_zone: str | None = None
    background_url: str | None = None
    overlay_url: str | None = None
    metadata_json: dict | None = None
    is_active: bool | None = None


class TouchMapRead(TimestampedModel):
    experience_id: str
    name: str
    floor_zone: str | None = None
    background_url: str | None = None
    overlay_url: str | None = None
    metadata_json: dict
    is_active: bool


class PlayerActivationRequest(BaseModel):
    channel_code: str
    hardware_id: str
    device_name: str
    app_version: str


class PlayerActivationResponse(BaseModel):
    player_token: str
    channel_id: str
    client_id: str
    branch_id: str
    activation_status: Literal["activation_new", "activation_existing"]


class PlayerTimeResponse(BaseModel):
    server_time: str
    unix_timestamp: int
    timezone: str


class PlayerHeartbeatCreate(BaseModel):
    status: str = "online"
    current_content_id: str | None = None
    current_campaign_id: str | None = None
    current_layout_id: str | None = None
    resolution: dict[str, Any] = Field(default_factory=dict)
    mode: str | None = None
    app_version: str | None = None
    cache_status: dict[str, Any] = Field(default_factory=dict)
    errors: list[Any] = Field(default_factory=list)
    local_time: datetime | None = None
    playback_position: dict[str, Any] = Field(default_factory=dict)


class PlayerHeartbeatAck(BaseModel):
    ok: bool
    received_at: datetime
    channel_id: str


class PlayerPlaybackEventCreate(BaseModel):
    content_id: str | None = None
    campaign_id: str | None = None
    layout_id: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    status: str = "completed"
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class PlayerPlaybackEventAck(BaseModel):
    event_id: str
    recorded_at: datetime
    channel_id: str
