"""
Shared column types.

``sa_type`` is annotated ``type[Any]`` in SQLModel's stubs but accepts a
``TypeEngine`` *instance* at runtime — which is the only way to ask for
``DateTime(timezone=True)``. Casting once here keeps ``mypy --strict`` honest
without a ``# type: ignore`` on every timestamp column in every model.

A ``TypeEngine`` instance is safe to share across columns and models (unlike a
``Column`` instance, which must never be reused from a mixin).
"""

from typing import Any, cast

from sqlalchemy import DateTime

#: ``TIMESTAMPTZ``. Every timestamp in this project is timezone-aware — see
#: AGENTS.md, "Things that will bite you": all bucketing is Asia/Dhaka, and a
#: naive column makes that impossible to get right.
TZDateTime: type[Any] = cast("type[Any]", DateTime(timezone=True))
