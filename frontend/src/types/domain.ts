export type UserRole =
  | "super_admin"
  | "staff_admin"
  | "staff_operator"
  | "client"
  | "client_admin"
  | "client_operator"
  | "branch_manager"
  | "operator";
export type UserStatus = "active" | "suspended";
export type ClientStatus = "active" | "suspended";
export type ChannelMode = "normal" | "expanded" | "videowall" | "audio" | "touch";
export type Orientation = "horizontal" | "vertical";
export type ContentType = "image" | "video" | "url" | "html" | "text" | "audio";
export type CampaignPlaybackMode = "sequential" | "random";
export type CampaignSequenceItemType = "content" | "layout";
export type LayoutTemplate = "single" | "two_columns" | "four_grid" | "custom";
export type ScheduleRecurrence = "once" | "daily" | "weekly" | "custom";
export type ChannelStatus = "online" | "offline" | "unknown";
export type KioskActionType = "open_url" | "switch_screen" | "play_video" | "navigate_menu";
export type AudioPlaylistKind = "music" | "spot";
export type AudioSpotRotationMode = "sequential" | "random";
export type AudioNormalizationStatus = "pending" | "normalized" | "skipped";
export type DataSourceType = "file_upload" | "google_sheets" | "api";
export type DataSourceStatus = "active" | "paused" | "error";
export type DatasetStatus = "ready" | "processing" | "error";
export type DatasetImportStatus = "pending" | "completed" | "failed";
export type DatasetColumnType = "text" | "number" | "datetime" | "boolean" | "empty";
export type LayoutBindingPreset = "autobuses" | "aeropuerto" | "menu" | "turnero";
export type LayoutWidgetType =
  | "image"
  | "video"
  | "text"
  | "clock"
  | "url"
  | "html"
  | "dataset_table"
  | "overlay_png"
  | "rss"
  | "weather"
  | "social_feed"
  | "api_feed"
  | "world_clock";
export type LayoutLayerRole = "background" | "content" | "overlay" | "branding" | "frame";
export type LayoutRevisionStatus = "draft" | "published" | "archived";
export type OutputMappingMode = "normal" | "sliced" | "custom";
export type OutputMappingProfile = "normal" | "led_wide" | "custom";
export type OutputMappingSliceDirection = "horizontal_stack" | "vertical_stack";

export interface OutputMappingSlice {
  slice_index: number;
  source_x: number;
  source_y: number;
  source_width: number;
  source_height: number;
  output_x: number;
  output_y: number;
  output_width: number;
  output_height: number;
  scale_x: number;
  scale_y: number;
}

export interface ChannelOutputMapping {
  enabled: boolean;
  profile: OutputMappingProfile;
  mode: OutputMappingMode;
  mapping_mode: OutputMappingMode;
  slice_count: number;
  slice_direction: OutputMappingSliceDirection;
  output_width: number;
  output_height: number;
  physical_width: number;
  physical_height: number;
  source_canvas_width: number;
  source_canvas_height: number;
  canvas_width: number;
  canvas_height: number;
  slices: OutputMappingSlice[];
  viewport_x: number;
  viewport_y: number;
  viewport_width: number;
  viewport_height: number;
  scale_x: number;
  scale_y: number;
}

export interface SessionUser {
  id: string;
  created_at: string;
  updated_at: string | null;
  email: string;
  full_name: string;
  role: UserRole;
  client_id: string | null;
  branch_id: string | null;
  is_active: boolean;
  status: UserStatus;
  last_login_at: string | null;
  permissions: string[];
}

export interface Client {
  id: string;
  created_at: string;
  updated_at: string | null;
  name: string;
  slug: string;
  contact_email: string | null;
  brand_name: string | null;
  status: ClientStatus;
}

export interface Branch {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  name: string;
  code: string;
  address: string | null;
  timezone: string;
  is_active: boolean;
}

export interface Layout {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  name: string;
  template: LayoutTemplate;
  canvas_width: number;
  canvas_height: number;
  zones: Array<Record<string, unknown>>;
  is_default: boolean;
}

export interface Channel {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  branch_id: string;
  name: string;
  channel_code: string;
  resolution_width: number;
  resolution_height: number;
  orientation: Orientation;
  mode: ChannelMode;
  screen_count: number;
  status: ChannelStatus;
  current_playback: string | null;
  hardware_identifier: string | null;
  expanded_outputs: Array<Record<string, unknown>>;
  output_mapping_json: ChannelOutputMapping;
  last_ping_at: string | null;
  last_heartbeat_at: string | null;
  heartbeat_age_seconds: number | null;
  is_online: boolean;
  notes: string | null;
}

export interface Campaign {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  layout_id: string | null;
  name: string;
  description: string | null;
  default_duration_seconds: number;
  is_active: boolean;
  loop_enabled: boolean;
  playback_mode: CampaignPlaybackMode;
}

export interface CampaignAssignmentResult {
  assignment_id: string;
  campaign_id: string;
  channel_id: string;
  assignment_status: "created" | "existing" | "replaced";
  detail: string;
}

export interface ContentItem {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  folder_id: string | null;
  name: string;
  type: ContentType;
  file_path: string | null;
  source_url: string | null;
  html_content: string | null;
  text_content: string | null;
  duration_seconds: number;
  metadata_json: Record<string, unknown>;
}

export interface ContentFolder {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  parent_id: string | null;
  name: string;
  sort_order: number;
  is_active: boolean;
}

export interface ContentFolderDeleteImpact {
  folder_id: string;
  folder_name: string;
  folder_count: number;
  subfolder_count: number;
  content_count: number;
  referenced_campaign_count: number;
  published_campaign_count: number;
  published_campaign_names: string[];
  has_published_content: boolean;
}

export interface PlaylistItem {
  id: string;
  created_at: string;
  updated_at: string | null;
  campaign_id: string;
  content_id: string;
  sort_order: number;
  duration_seconds: number;
  zone_key: string;
}

export interface CampaignSequenceItem {
  id: string;
  created_at: string;
  updated_at: string | null;
  campaign_id: string;
  item_type: CampaignSequenceItemType;
  content_id: string | null;
  layout_id: string | null;
  sort_order: number;
  duration_seconds: number;
  options_json: Record<string, unknown>;
  is_enabled: boolean;
}

export interface CampaignSequencePreviewLayout {
  id: string;
  name: string;
  template: LayoutTemplate;
  canvas_width: number;
  canvas_height: number;
  zones: Array<Record<string, unknown>>;
  is_default: boolean;
}

export interface CampaignSequencePreviewResolvedItem extends CampaignSequenceItem {
  zone_key: string;
  content: PlayerRuntimeContent | null;
  layout: CampaignSequencePreviewLayout | null;
}

export interface CampaignSequencePreviewPayload {
  campaign: Campaign;
  playback_mode: CampaignPlaybackMode;
  loop_enabled: boolean;
  sequence_items: CampaignSequenceItem[];
  resolved_items: CampaignSequencePreviewResolvedItem[];
  contents: PlayerRuntimeContent[];
  layouts: CampaignSequencePreviewLayout[];
  campaign_layout: CampaignSequencePreviewLayout | null;
  duration_total: number;
  runtime_signature: string;
  generated_at: string;
  playlist_legacy: PlayerRuntimePlaylistItem[];
}

export interface PlayerRuntimeContent {
  id: string;
  name: string;
  type: ContentType;
  file_path: string | null;
  download_url: string | null;
  source_url: string | null;
  html_content: string | null;
  text_content: string | null;
  mime_type: string | null;
  duration_seconds: number | null;
  checksum: string | null;
  size: number | null;
  metadata_json: Record<string, unknown>;
}

export interface PlayerRuntimePlaylistItem {
  id: string;
  campaign_id: string;
  content_id: string;
  sort_order: number;
  duration_seconds: number;
  zone_key: string;
  content: PlayerRuntimeContent | null;
}

export interface PlayerRuntimeCampaignSummary {
  campaign_id: string;
  name: string;
  description: string | null;
  layout_id: string | null;
  source: string;
  priority: number;
  playlist_items_count: number;
  schedule_id?: string;
  assignment_id?: string;
}

export interface PlayerRuntimeVideowallNode {
  position_index: number;
  row: number;
  column: number;
  row_index: number;
  column_index: number;
  width: number;
  height: number;
  crop_x: number;
  crop_y: number;
  crop_width: number;
  crop_height: number;
  crop: {
    x: number;
    y: number;
    width: number;
    height: number;
  };
}

export interface PlayerRuntimeVideowallState {
  enabled: boolean;
  wall_id?: string;
  videowall_id?: string;
  name?: string;
  columns?: number;
  rows?: number;
  total_width?: number;
  total_height?: number;
  sync_group?: string;
  sync_mode?: string;
  start_tolerance_ms?: number;
  server_time?: string;
  play_at_timestamp?: number;
  sync?: {
    mode: string;
    loop_start_epoch_ms: number | null;
    tolerance_ms: number;
  };
  node?: PlayerRuntimeVideowallNode | null;
}

export interface PlayerRuntimeConfig {
  runtime_version: number;
  runtime_generated_at: string;
  server_time: string;
  play_at_timestamp: number;
  channel: Channel;
  branch: Branch | null;
  client: Client | null;
  mode: ChannelMode;
  playback_mode?: CampaignPlaybackMode;
  schedule: ScheduleItem | null;
  campaigns: PlayerRuntimeCampaignSummary[];
  sequence_items?: Array<Record<string, unknown>>;
  playlist: PlayerRuntimePlaylistItem[];
  contents: PlayerRuntimeContent[];
  layout: Layout | null;
  layout_runtime_snapshot: Record<string, unknown> | null;
  dataset_runtime_data: Record<string, unknown> | null;
  videowall: PlayerRuntimeVideowallState | null;
  output_mapping: ChannelOutputMapping;
  audio: Record<string, unknown> | null;
  touch: Record<string, unknown> | null;
  fallback: Record<string, unknown> | null;
}

export interface ScheduleItem {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  campaign_id: string;
  channel_id: string | null;
  branch_id: string | null;
  layout_id: string | null;
  title: string;
  recurrence: ScheduleRecurrence;
  days_of_week: number[];
  starts_on: string | null;
  ends_on: string | null;
  start_time: string | null;
  end_time: string | null;
  is_active: boolean;
  is_looping: boolean;
  timezone: string;
  priority: number;
}

export interface Videowall {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  name: string;
  columns: number;
  rows: number;
  total_width: number;
  total_height: number;
  start_tolerance_ms: number;
  sync_mode: string;
}

export interface VideowallNode {
  id: string;
  created_at: string;
  updated_at: string | null;
  videowall_id: string;
  channel_id: string;
  position_index: number;
  row_index: number;
  column_index: number;
  x: number;
  y: number;
  width: number;
  height: number;
  crop_x?: number;
  crop_y?: number;
  crop_width?: number;
  crop_height?: number;
}

export interface KioskScreen {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  experience_id: string | null;
  name: string;
  slug: string;
  background_url: string | null;
  attract_media_url: string | null;
  inactivity_timeout_seconds: number;
  is_attract_screen: boolean;
  screen_kind: string;
  sort_order: number;
  metadata_json: Record<string, unknown>;
  idle_timeout_override: number | null;
}

export interface KioskButton {
  id: string;
  created_at: string;
  updated_at: string | null;
  screen_id: string;
  label: string;
  x: number;
  y: number;
  width: number;
  height: number;
  action_type: KioskActionType;
  action_value: string | null;
  target_screen_id: string | null;
  sort_order: number;
  style_json: Record<string, unknown>;
  action_payload_json: Record<string, unknown>;
  is_hotspot: boolean;
}

export interface TouchExperience {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  name: string;
  slug: string;
  description: string | null;
  attract_screen_id: string | null;
  home_screen_id: string | null;
  default_idle_timeout_seconds: number;
  is_active: boolean;
  metadata_json: Record<string, unknown>;
}

export interface TouchExperienceAssignment {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  experience_id: string;
  branch_id: string | null;
  channel_id: string | null;
  sort_order: number;
  is_active: boolean;
  metadata_json: Record<string, unknown>;
}

export interface TouchLocation {
  id: string;
  created_at: string;
  updated_at: string | null;
  experience_id: string;
  name: string;
  category: string | null;
  description: string | null;
  floor_zone: string | null;
  suite: string | null;
  image_url: string | null;
  metadata_json: Record<string, unknown>;
  is_active: boolean;
}

export interface TouchMap {
  id: string;
  created_at: string;
  updated_at: string | null;
  experience_id: string;
  name: string;
  floor_zone: string | null;
  background_url: string | null;
  overlay_url: string | null;
  metadata_json: Record<string, unknown>;
  is_active: boolean;
}

export interface TouchRuntimeScreen extends KioskScreen {
  buttons: KioskButton[];
}

export interface TouchRuntimeConfig {
  experience: TouchExperience;
  screens: TouchRuntimeScreen[];
  locations: TouchLocation[];
  maps: TouchMap[];
  assignments: TouchExperienceAssignment[];
  player_ready: boolean;
  touch_runtime_version: number;
}

export interface AudioLibraryItem {
  id: string;
  name: string;
  client_id: string;
  file_path: string | null;
  duration_seconds: number;
  audio_kind: AudioPlaylistKind;
  normalization_status: AudioNormalizationStatus;
  target_lufs: number;
  volume_normalized: boolean;
  metadata_json: Record<string, unknown>;
}

export interface AudioPlaylist {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  name: string;
  kind: AudioPlaylistKind;
  description: string | null;
  is_active: boolean;
}

export interface AudioPlaylistItem {
  id: string;
  playlist_id: string;
  content_id: string;
  sort_order: number;
  is_enabled: boolean;
  content: AudioLibraryItem | null;
}

export interface AudioAssignment {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  branch_id: string | null;
  channel_id: string | null;
  music_playlist_id: string | null;
  spot_playlist_id: string | null;
  songs_between_spots: number;
  spots_per_break: number;
  spot_rotation_mode: AudioSpotRotationMode;
  avoid_consecutive_spots: boolean;
  volume_normalization_enabled: boolean;
  volume_normalization_status: AudioNormalizationStatus;
  target_lufs: number;
}

export interface AudioReportEntry {
  content_id: string | null;
  content_name: string;
  playlist_id: string | null;
  playlist_name: string | null;
  play_count: number;
  last_played_at: string | null;
}

export interface AudioRecentEvent {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  branch_id: string | null;
  channel_id: string | null;
  playlist_id: string | null;
  content_id: string | null;
  entry_kind: AudioPlaylistKind;
  played_at: string;
  content_name: string;
  playlist_name: string | null;
}

export interface AudioReportSummary {
  music: AudioReportEntry[];
  spots: AudioReportEntry[];
  recent_events: AudioRecentEvent[];
}

export interface AudioRuntimePlaylist extends AudioPlaylist {
  items: AudioPlaylistItem[];
}

export interface AudioRuntimeConfig {
  channel: {
    id: string;
    name: string;
    mode: ChannelMode;
    branch_id: string;
    status: ChannelStatus;
  };
  assignment_scope: "channel" | "branch" | "none";
  audio_enabled: boolean;
  music_playlist: AudioRuntimePlaylist | null;
  spot_playlist: AudioRuntimePlaylist | null;
  rules: {
    songs_between_spots: number;
    spots_per_break: number;
    spot_rotation_mode: AudioSpotRotationMode;
    avoid_consecutive_spots: boolean;
  };
  normalization: {
    enabled: boolean;
    status: AudioNormalizationStatus;
    target_lufs: number;
  };
}

export interface DataSource {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  name: string;
  source_type: DataSourceType;
  status: DataSourceStatus;
  description: string | null;
  config_json: Record<string, unknown>;
  is_active: boolean;
}

export interface Dataset {
  id: string;
  created_at: string;
  updated_at: string | null;
  client_id: string;
  data_source_id: string;
  name: string;
  slug: string;
  description: string | null;
  status: DatasetStatus;
  current_import_id: string | null;
  row_count: number;
  column_count: number;
}

export interface DatasetImport {
  id: string;
  created_at: string;
  updated_at: string | null;
  dataset_id: string;
  source_filename: string;
  source_mime_type: string | null;
  storage_path: string;
  import_status: DatasetImportStatus;
  detected_sheet_name: string | null;
  row_count: number;
  column_count: number;
  imported_at: string;
  summary_json: Record<string, unknown>;
}

export interface DatasetColumn {
  id: string;
  created_at: string;
  updated_at: string | null;
  dataset_id: string;
  import_id: string;
  column_key: string;
  display_name: string;
  source_name: string;
  data_type: DatasetColumnType;
  position_index: number;
  sample_value: string | null;
  is_visible: boolean;
}

export interface DatasetRow {
  id: string;
  created_at: string;
  updated_at: string | null;
  dataset_id: string;
  import_id: string;
  row_index: number;
  row_data_json: Record<string, unknown>;
  row_hash: string | null;
}

export interface DatasetPreview {
  dataset: Dataset;
  current_import: DatasetImport | null;
  columns: DatasetColumn[];
  rows: DatasetRow[];
}

export interface LayoutDataBinding {
  id: string;
  created_at: string;
  updated_at: string | null;
  layout_id: string;
  dataset_id: string;
  name: string;
  preset_key: LayoutBindingPreset;
  zone_key: string | null;
  sort_order: number;
  max_rows: number;
  options_json: Record<string, unknown>;
  is_active: boolean;
}

export interface LayoutBindingField {
  id: string;
  created_at: string;
  updated_at: string | null;
  binding_id: string;
  target_field: string;
  column_key: string | null;
  display_label: string | null;
  fallback_value: string | null;
  format_hint: string | null;
  position_index: number;
  is_required: boolean;
  options_json: Record<string, unknown>;
}

export interface LayoutBindingPresetField {
  key: string;
  label: string;
  required: boolean;
  format_hint?: string | null;
}

export interface LayoutBindingPresetInfo {
  key: LayoutBindingPreset;
  label: string;
  description: string;
  default_max_rows: number;
  fields: LayoutBindingPresetField[];
}

export interface LayoutBindingPreviewHeader {
  key: string;
  label: string;
  target_field: string;
  column_key: string | null;
  required: boolean;
  format_hint: string | null;
}

export interface LayoutBindingValidation {
  valid: boolean;
  errors: string[];
  warnings: string[];
  missing_required_fields: string[];
}

export interface LayoutBindingMappedField {
  target_field: string;
  label: string;
  required: boolean;
  column_key: string | null;
  column_label: string | null;
  data_type: DatasetColumnType | null;
  fallback_value: string | null;
  format_hint: string | null;
  position_index: number;
  resolved: boolean;
  options_json: Record<string, unknown>;
}

export interface LayoutPreviewZone {
  key: string;
  label: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LayoutBindingPreviewResult {
  binding: LayoutDataBinding | null;
  preset: LayoutBindingPresetInfo;
  dataset: Dataset | null;
  current_import: DatasetImport | null;
  zone: LayoutPreviewZone | null;
  fields: Array<LayoutBindingField | Record<string, unknown>>;
  mapped_fields: LayoutBindingMappedField[];
  headers: LayoutBindingPreviewHeader[];
  rows: Array<Record<string, unknown>>;
  validation: LayoutBindingValidation;
}

export interface LayoutDataPreviewPayload {
  layout: Layout;
  zones: LayoutPreviewZone[];
  bindings: LayoutBindingPreviewResult[];
  generated_at: string;
}

export interface LayoutRuntimeDataPayload extends LayoutDataPreviewPayload {
  runtime_version: number;
  player_ready: boolean;
}

export interface LayoutRegion {
  id: string;
  key: string;
  label: string;
  x: number;
  y: number;
  width: number;
  height: number;
  z_index: number;
  layer_role: LayoutLayerRole;
  visible: boolean;
  locked: boolean;
}

export interface LayoutWidgetBindingRef {
  binding_id: string | null;
  dataset_id: string | null;
}

export interface LayoutWidget {
  id: string;
  widget_type: LayoutWidgetType;
  name: string;
  region_id: string;
  layer_role: LayoutLayerRole;
  x: number;
  y: number;
  width: number;
  height: number;
  z_index: number;
  opacity: number;
  rotation: number;
  visible: boolean;
  locked: boolean;
  props_json: Record<string, unknown>;
  binding_ref: LayoutWidgetBindingRef;
}

export interface LayoutEditorState {
  version: number;
  canvas: {
    width: number;
    height: number;
    background_color: string;
  };
  guides: {
    show_grid: boolean;
    show_safe_area: boolean;
    snap_threshold: number;
  };
  supported_widget_types: LayoutWidgetType[];
  future_widget_types: LayoutWidgetType[];
  layer_roles: LayoutLayerRole[];
  regions: LayoutRegion[];
  widgets: LayoutWidget[];
  settings: Record<string, unknown>;
}

export interface LayoutRevision {
  id: string;
  created_at: string;
  updated_at: string | null;
  layout_id: string;
  revision_number: number;
  name: string;
  status: LayoutRevisionStatus;
  notes: string | null;
  editor_state_json: LayoutEditorState;
  preview_state_json: Record<string, unknown>;
  published_at: string | null;
  created_by_user_id: string | null;
  is_current_draft: boolean;
  is_current_published: boolean;
}

export interface LayoutEditorPayload {
  layout: Layout;
  active_revision: LayoutRevision;
  revisions: LayoutRevision[];
  supported_widget_types: LayoutWidgetType[];
  future_widget_types: LayoutWidgetType[];
  layer_roles: LayoutLayerRole[];
  binding_preview: LayoutDataPreviewPayload;
  editor_generated_at: string;
  player_ready: boolean;
}

export interface LayoutPreviewPayload {
  layout: Layout;
  revision: LayoutRevision;
  preview: {
    version: number;
    canvas: {
      width: number;
      height: number;
      background_color: string;
    };
    guides: {
      show_grid: boolean;
      show_safe_area: boolean;
      snap_threshold: number;
    };
    regions: LayoutRegion[];
    widgets: Array<LayoutWidget & { binding_preview?: LayoutBindingPreviewResult }>;
    bindings: LayoutBindingPreviewResult[];
    generated_at: string;
    player_ready: boolean;
  };
  preview_generated_at: string;
  player_ready: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  user: SessionUser;
}
