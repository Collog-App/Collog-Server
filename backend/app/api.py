from fastapi import APIRouter

from app.auth_router import router as auth_router
from app.routes.analysis import router as analysis_router
from app.routes.call_audio import router as call_audio_router
from app.routes.calls import router as calls_router
from app.routes.consents import router as consents_router
from app.routes.devices import router as devices_router
from app.routes.families import router as families_router
from app.routes.profiles import router as profiles_router
from app.routes.questions import router as questions_router
from app.routes.reports import router as reports_router
from app.routes.webhooks import router as webhooks_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(devices_router)
router.include_router(families_router)
router.include_router(consents_router)
router.include_router(profiles_router)
router.include_router(questions_router)
router.include_router(calls_router)
router.include_router(call_audio_router)
router.include_router(analysis_router)
router.include_router(reports_router)
router.include_router(webhooks_router)
