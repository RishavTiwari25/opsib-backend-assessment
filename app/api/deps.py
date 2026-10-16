"""Application container and request dependencies."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.ai.summarizer import ClaudeSummarizer, SummaryProvider
from app.core.clock import Clock, clock_from_setting
from app.core.config import Settings
from app.core.errors import ValidationFailed
from app.db.session import make_engine, make_session_factory
from app.detection.service import DetectionConfig
from app.routing.policy import RoutingPolicy, default_policy


@dataclass
class Container:
    settings: Settings
    engine: Engine
    factory: sessionmaker[Session]
    clock: Clock
    policy: RoutingPolicy
    ai_provider: SummaryProvider | None

    @property
    def tenant_id(self) -> str:
        return self.settings.tenant_id

    def detection_config(self) -> DetectionConfig:
        return DetectionConfig(
            tolerance=self.settings.shortfall_tolerance_base_units,
            task_due_hours=self.settings.task_due_hours,
            policy=self.policy,
        )


def build_container(settings: Settings, *, clock: Clock | None = None, ai_provider: SummaryProvider | None = None) -> Container:
    engine = make_engine(settings.database_url)
    provider = ai_provider
    if provider is None and settings.ai_provider == "claude" and settings.anthropic_api_key is not None:
        provider = ClaudeSummarizer(settings.anthropic_api_key.get_secret_value(), settings.anthropic_model)
    return Container(
        settings=settings,
        engine=engine,
        factory=make_session_factory(engine),
        clock=clock or clock_from_setting(settings.evaluation_time),
        policy=default_policy(settings.availability_max_age_minutes),
        ai_provider=provider,
    )


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_actor(x_actor_id: str | None = Header(default=None, description="Acting employee id (stand-in for an authenticated principal)")) -> str:
    if not x_actor_id or not x_actor_id.strip():
        raise ValidationFailed("X-Actor-Id header is required for this action", code="ACTOR_REQUIRED")
    return x_actor_id.strip()
