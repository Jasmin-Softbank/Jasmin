from __future__ import annotations

from typing import Protocol
from control_plane.domain.models import (Accepted, CreateServerSpec, DeleteResult, Flavor, Image, Network, Page, PageRequest, RequestContext, Server, ServerAction)

class HealthProvider(Protocol):
    def check_ready(self, ctx: RequestContext) -> bool: ...

