from __future__ import annotations

from app.config import Settings
from app.database import Database
from app.services.acoustics import create_acoustic_analyzer
from app.services.apple_auth import AppleIdentityVerifier
from app.services.calls import CallLifecycle
from app.services.deepgram import create_stt_gateway
from app.services.gemini import create_extraction_gateway
from app.services.livekit import create_livekit_gateway
from app.services.notifications import create_report_push_gateway, create_voip_push_gateway
from app.services.pipeline import ProcessingPipeline
from app.services.report_notifications import ReportNotifications
from app.services.reports import ReportService
from app.services.signals import SignalService
from app.services.storage import create_storage
from app.services.tts import create_question_tts_gateway


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.apple_identity = AppleIdentityVerifier(settings)
        self.database = Database(settings.database_url)
        self.storage = create_storage(settings)
        self.question_tts = create_question_tts_gateway(settings, self.storage)
        self.livekit = create_livekit_gateway(settings)
        self.calls = CallLifecycle(settings, self.database, self.livekit, self.storage)
        self.voip_push = create_voip_push_gateway(settings)
        self.report_push = create_report_push_gateway(settings)
        self.report_notifications = ReportNotifications(self.database, self.report_push)
        self.stt = create_stt_gateway(settings)
        self.extraction = create_extraction_gateway(settings)
        self.acoustics = create_acoustic_analyzer(settings)
        self.signals = SignalService(settings)
        self.reports = ReportService()
        self.pipeline = ProcessingPipeline(
            settings,
            self.database,
            self.storage,
            self.stt,
            self.extraction,
            self.acoustics,
            self.signals,
        )
