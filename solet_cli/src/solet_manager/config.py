"""Closed, non-secret TOML configuration and flag precedence."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config_loading import load_config_values
from .models import JsonValue


@dataclass(frozen=True)
class CreateConfig:
    """Resolved create inputs after flags > TOML > defaults."""

    name: str
    target: Path
    autostart: bool
    decisions: dict[str, JsonValue] = field(default_factory=dict)
    decision_sources: dict[str, str] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "target": str(self.target),
            "autostart": self.autostart,
            "decisions": self.decisions,
        }

    def to_identity_dict(self) -> dict[str, JsonValue]:
        """Return immutable transaction identity, excluding staged answer resolution."""

        return {
            "name": self.name,
            "target": str(self.target),
        }


def load_create_config(
    *,
    config_path: Path | None,
    flag_name: str | None,
    flag_target: Path | None,
    flag_autostart: bool | None,
    flag_decisions: dict[str, JsonValue] | None = None,
    home: Path | None = None,
) -> CreateConfig:
    """Load and resolve the accepted v1 configuration surface."""

    name_value, target, autostart, decisions, sources = load_config_values(
        config_path=config_path,
        flag_name=flag_name,
        flag_target=flag_target,
        flag_autostart=flag_autostart,
        flag_decisions=flag_decisions,
        home=home,
    )
    return CreateConfig(
        name=name_value,
        target=target,
        autostart=autostart,
        decisions=decisions,
        decision_sources=sources,
    )
