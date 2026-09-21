"""
device/session.py

Local session state on the device.
Tracks which exhibit the visitor is currently near,
session ID for correlation, and exhibits visited during this tour.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class SessionState:
    device_id: str
    museum_id: str

    session_id: str = field(default_factory=lambda: f"sess-{uuid.uuid4().hex[:10]}")
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    current_beacon_uuid: Optional[str] = None
    current_exhibit_id: Optional[str] = None
    current_exhibit_title: Optional[str] = None

    exhibits_visited: list[str] = field(default_factory=list)
    is_speaking: bool = False        # True while TTS is playing (block new triggers)

    def enter_exhibit(self, beacon_uuid: str, exhibit_id: str, title: str):
        self.current_beacon_uuid = beacon_uuid
        self.current_exhibit_id = exhibit_id
        self.current_exhibit_title = title
        if exhibit_id not in self.exhibits_visited:
            self.exhibits_visited.append(exhibit_id)

    def leave_exhibit(self):
        self.current_beacon_uuid = None
        self.current_exhibit_id = None
        self.current_exhibit_title = None

    @property
    def has_exhibit(self) -> bool:
        return self.current_exhibit_id is not None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "device_id": self.device_id,
            "museum_id": self.museum_id,
            "exhibit_id": self.current_exhibit_id,
            "exhibit_title": self.current_exhibit_title,
        }
