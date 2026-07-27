from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from workflow.config import Settings
from workflow.db.models import ActorProfile, Role


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def sync_actor_profiles(session: Session, settings: Settings) -> list[ActorProfile]:
    synced: list[ActorProfile] = []
    identities: set[tuple[str, Role, str]] = set()
    for actor in settings.actors():
        role = Role(actor.role)
        identity = (settings.company_id, role, actor.name)
        identities.add(identity)
        profile = session.scalar(
            select(ActorProfile).where(
                ActorProfile.company_id == settings.company_id,
                ActorProfile.role == role,
                ActorProfile.display_name == actor.name,
            )
        )
        if profile is None:
            profile = ActorProfile(
                company_id=settings.company_id,
                display_name=actor.name,
                role=role,
                position="老板" if role == Role.BOSS else "新媒体运营",
                token_sha256=token_digest(actor.token),
            )
            session.add(profile)
        profile.active = actor.active
        profile.wecom_userid = actor.wecom_userid
        profile.token_sha256 = token_digest(actor.token)
        synced.append(profile)

    existing = session.scalars(
        select(ActorProfile).where(ActorProfile.company_id == settings.company_id)
    )
    for profile in existing:
        if (profile.company_id, profile.role, profile.display_name) not in identities:
            profile.active = False
    session.flush()
    return synced


def find_actor_by_token(session: Session, settings: Settings, token: str) -> ActorProfile | None:
    return session.scalar(
        select(ActorProfile).where(
            ActorProfile.company_id == settings.company_id,
            ActorProfile.token_sha256 == token_digest(token),
            ActorProfile.active.is_(True),
        )
    )
