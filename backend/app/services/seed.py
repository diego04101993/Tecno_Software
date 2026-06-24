from datetime import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.entities import Branch, Campaign, CampaignPlaylistItem, Channel, ChannelConnectionStatus, ChannelMode, Client, ContentItem, ContentType, KioskActionType, KioskButton, KioskScreen, Layout, LayoutTemplate, Orientation, Schedule, ScheduleRecurrence, User, UserRole, Videowall, VideowallNode


def seed_initial_data(db: Session) -> None:
    existing_admin = db.scalar(select(User).where(User.email == "superadmin@tecnocontrol.com"))
    existing_client = db.scalar(select(Client).where(Client.slug == "demo-retail"))
    if existing_admin:
        return

    if existing_client:
        super_admin = User(
            email="superadmin@tecnocontrol.com",
            full_name="Tecno Control",
            password_hash=hash_password("ChangeMe123!"),
            role=UserRole.SUPER_ADMIN,
            status="active",
            is_active=True,
        )
        db.add(super_admin)
        db.commit()
        return

    client = Client(
        name="Demo Retail",
        slug="demo-retail",
        contact_email="ops@demo-retail.local",
        brand_name="Demo Retail",
    )
    db.add(client)
    db.flush()

    super_admin = User(
        email="superadmin@tecnocontrol.com",
        full_name="Tecno Control",
        password_hash=hash_password("ChangeMe123!"),
        role=UserRole.SUPER_ADMIN,
        status="active",
        is_active=True,
    )
    client_admin = User(
        email="demo@demo-retail.com",
        full_name="Demo Retail Admin",
        password_hash=hash_password("ChangeMe123!"),
        role=UserRole.CLIENT_ADMIN,
        client_id=client.id,
        status="active",
        is_active=True,
    )
    branch = Branch(
        client_id=client.id,
        name="Sucursal Centro",
        code="CENTRO",
        address="Av. Reforma 100, CDMX",
    )
    layout = Layout(
        client_id=client.id,
        name="Menuboard doble",
        template=LayoutTemplate.TWO_COLUMNS,
        canvas_width=3840,
        canvas_height=1080,
        zones=[
            {"key": "left", "x": 0, "y": 0, "width": 1920, "height": 1080},
            {"key": "right", "x": 1920, "y": 0, "width": 1920, "height": 1080},
        ],
        is_default=True,
    )
    db.add_all([super_admin, client_admin, branch, layout])
    db.flush()

    channel_primary = Channel(
        client_id=client.id,
        branch_id=branch.id,
        name="Pantalla caja 1",
        mode=ChannelMode.NORMAL,
        orientation=Orientation.HORIZONTAL,
        status=ChannelConnectionStatus.ONLINE,
        current_playback="Promociones Mayo / Combo Desayuno",
    )
    channel_menu = Channel(
        client_id=client.id,
        branch_id=branch.id,
        name="Menu Board 2x1",
        mode=ChannelMode.EXPANDED,
        screen_count=2,
        resolution_width=3840,
        resolution_height=1080,
        orientation=Orientation.HORIZONTAL,
    )
    channel_wall = Channel(
        client_id=client.id,
        branch_id=branch.id,
        name="Videowall Lobby",
        mode=ChannelMode.VIDEOWALL,
        screen_count=4,
        resolution_width=3840,
        resolution_height=2160,
    )
    db.add_all([channel_primary, channel_menu, channel_wall])
    db.flush()

    campaign = Campaign(
        client_id=client.id,
        layout_id=layout.id,
        name="Promociones Mayo",
        description="Campaña base con combos, promociones y llamado a kiosko.",
    )
    content_text = ContentItem(
        client_id=client.id,
        name="Combo Desayuno",
        type=ContentType.TEXT,
        text_content="Combo desayuno desde $89 MXN",
        duration_seconds=12,
    )
    content_url = ContentItem(
        client_id=client.id,
        name="Menú web",
        type=ContentType.URL,
        source_url="https://demo-retail.local/menu",
        duration_seconds=20,
    )
    db.add_all([campaign, content_text, content_url])
    db.flush()

    playlist = [
        CampaignPlaylistItem(campaign_id=campaign.id, content_id=content_text.id, sort_order=1, duration_seconds=12),
        CampaignPlaylistItem(campaign_id=campaign.id, content_id=content_url.id, sort_order=2, duration_seconds=20),
    ]
    db.add_all(playlist)

    schedule = Schedule(
        client_id=client.id,
        campaign_id=campaign.id,
        channel_id=channel_primary.id,
        layout_id=layout.id,
        title="Horario general tienda",
        recurrence=ScheduleRecurrence.DAILY,
        days_of_week=[1, 2, 3, 4, 5, 6, 7],
        start_time=time(hour=8, minute=0),
        end_time=time(hour=22, minute=0),
        is_looping=True,
    )
    videowall = Videowall(
        client_id=client.id,
        name="Lobby 2x2",
        columns=2,
        rows=2,
        total_width=3840,
        total_height=2160,
    )
    db.add_all([schedule, videowall])
    db.flush()

    nodes = [
        VideowallNode(videowall_id=videowall.id, channel_id=channel_wall.id, position_index=1, row_index=0, column_index=0, x=0, y=0, width=1920, height=1080),
    ]
    db.add_all(nodes)

    attract_screen = KioskScreen(
        client_id=client.id,
        name="Attract",
        slug="attract",
        background_url="https://images.unsplash.com/photo-1556740749-887f6717d7e4",
        attract_media_url="https://images.unsplash.com/photo-1495474472287-4d71bcdd2085",
        is_attract_screen=True,
    )
    menu_screen = KioskScreen(
        client_id=client.id,
        name="Menu Principal",
        slug="menu-principal",
        background_url="https://images.unsplash.com/photo-1515003197210-e0cd71810b5f",
        inactivity_timeout_seconds=45,
    )
    db.add_all([attract_screen, menu_screen])
    db.flush()

    db.add(
        KioskButton(
            screen_id=menu_screen.id,
            label="Promociones",
            x=180,
            y=220,
            width=280,
            height=88,
            action_type=KioskActionType.SWITCH_SCREEN,
            target_screen_id=attract_screen.id,
            sort_order=1,
        )
    )
    db.commit()

