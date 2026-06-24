from fastapi import APIRouter

from app.api.routes import admin, audio, auth, branches, campaigns, channels, clients, content_folders, contents, data_sources, kiosk, layouts, player, realtime, schedules, team, touch, users, videowalls


api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(team.router, prefix="/team", tags=["team"])
api_router.include_router(clients.router, prefix="/clients", tags=["clients"])
api_router.include_router(branches.router, prefix="/branches", tags=["branches"])
api_router.include_router(channels.router, prefix="/channels", tags=["channels"])
api_router.include_router(campaigns.router, prefix="/campaigns", tags=["campaigns"])
api_router.include_router(contents.router, prefix="/contents", tags=["contents"])
api_router.include_router(content_folders.router, tags=["content-folders"])
api_router.include_router(data_sources.router, tags=["data-sources"])
api_router.include_router(audio.router, prefix="/audio", tags=["audio"])
api_router.include_router(player.router, prefix="/player", tags=["player"])
api_router.include_router(schedules.router, prefix="/schedules", tags=["schedules"])
api_router.include_router(layouts.router, prefix="/layouts", tags=["layouts"])
api_router.include_router(videowalls.router, prefix="/videowalls", tags=["videowalls"])
api_router.include_router(kiosk.router, prefix="/kiosk", tags=["kiosk"])
api_router.include_router(touch.router, prefix="/touch", tags=["touch"])
api_router.include_router(realtime.router, prefix="/realtime", tags=["realtime"])
