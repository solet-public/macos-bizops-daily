"""Closed TOML parsing and create-input precedence resolution."""

from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path

from .errors import ConfigError
from .models import JsonValue

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_ALLOWED_KEYS = frozenset(
    {"schema_version", "name", "target", "autostart", "decisions"}
)
_RESERVED_DECISIONS = frozenset({"setup_profile", "autostart"})
_SECRET_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "credential",
    "oauth",
    "private_key",
)


def load_config_values(
    *,
    config_path: Path | None,
    flag_name: str | None,
    flag_target: Path | None,
    flag_autostart: bool | None,
    flag_decisions: dict[str, JsonValue] | None,
    home: Path | None,
) -> tuple[str, Path, bool, dict[str, JsonValue], dict[str, str]]:
    raw = read_config(config_path) if config_path is not None else {}
    name = resolve_name(flag_name, raw)
    user_home = (Path.home() if home is None else home).expanduser().resolve(
        strict=False
    )
    target = resolve_target(flag_target, raw, user_home, name)
    autostart = resolve_autostart(flag_autostart, raw)
    config_decisions = decision_table(raw.get("decisions"), source="config")
    flag_values = decision_table(flag_decisions, source="flag")
    decisions = {**config_decisions, **flag_values}
    sources = dict.fromkeys(config_decisions, "config")
    sources.update(dict.fromkeys(flag_values, "flag"))
    return name, target, autostart, decisions, sources


def read_config(path: Path) -> dict[str, JsonValue]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config {path} is not valid TOML: {exc}") from exc
    _validate_config_keys(payload)
    if payload.get("schema_version") != 1:
        raise ConfigError("config schema_version must be exactly 1")
    return payload


def _validate_config_keys(payload: dict[str, JsonValue]) -> None:
    unknown = sorted(set(payload) - _ALLOWED_KEYS)
    if not unknown:
        return
    secret_like = [key for key in unknown if _is_secret_like(key)]
    if secret_like:
        raise ConfigError(
            f"secret fields are forbidden in manager config: {', '.join(secret_like)}",
            repair="Configure credentials later through the target-local connector flow.",
        )
    raise ConfigError(f"unknown config keys: {', '.join(unknown)}")


def resolve_name(flag_name: str | None, raw: dict[str, JsonValue]) -> str:
    value = flag_name if flag_name is not None else raw.get("name")
    if not isinstance(value, str) or not _NAME_PATTERN.fullmatch(value):
        raise ConfigError(
            "name is required and must match ^[a-z][a-z0-9_-]{1,31}$",
            repair="Pass a lowercase name such as 'bizops'.",
        )
    return value


def validate_name_path_collision(
    *,
    name: str,
    target: Path,
    is_matching_resume: bool,
) -> None:
    """Refuse a PATH collision unless it is this transaction's named launcher.

    The caller may set ``is_matching_resume`` only after it has validated the
    persisted transaction identity.  Keeping that lifecycle fact at the call
    site prevents a first create from treating an arbitrary PATH command as an
    already-owned launcher.
    """

    collision = shutil.which(name)
    if collision is None:
        return
    if is_matching_resume and _is_own_named_launcher(
        collision=Path(collision),
        name=name,
        target=target,
    ):
        return
    raise ConfigError(
        f"name {name!r} collides with the PATH command {collision!r}",
        repair=(
            "Choose a name that does not resolve on PATH; its named launcher "
            "will occupy that command."
        ),
    )


def _is_own_named_launcher(*, collision: Path, name: str, target: Path) -> bool:
    """Return whether the PATH hit is the installed launcher for ``target``.

    Target genesis exposes no launcher-directory input: it installs the named
    launcher at ``Path.home() / '.local/bin' / name``.  Require both that
    canonical path and its strict target identity so another PATH command is
    never mistaken for the instance's launcher.
    """

    expected_launcher = Path.home() / ".local" / "bin" / name
    expected_bridge = target / ".venv" / "bin" / "solet-bridge"
    try:
        return (
            collision == expected_launcher
            and collision.is_symlink()
            and collision.resolve(strict=True) == expected_bridge.resolve(strict=True)
        )
    except OSError:
        return False


def resolve_target(
    flag_target: Path | None,
    raw: dict[str, JsonValue],
    user_home: Path,
    name: str,
) -> Path:
    value = flag_target if flag_target is not None else raw.get("target")
    target = _target_path(value, user_home, name).expanduser().resolve(strict=False)
    if not target.is_absolute() or target in {Path("/"), user_home}:
        raise ConfigError(
            f"target must be an absolute, instance-specific path: {target}"
        )
    return target


def _target_path(value: JsonValue | Path, user_home: Path, name: str) -> Path:
    if value is None:
        return user_home / "Solets" / name
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        return Path(value)
    raise ConfigError("target must be a path string")


def resolve_autostart(
    flag_autostart: bool | None,
    raw: dict[str, JsonValue],
) -> bool:
    raw_value = raw.get("autostart", True)
    value = flag_autostart if flag_autostart is not None else raw_value
    if not isinstance(value, bool):
        raise ConfigError("autostart must be true or false")
    return value


def decision_table(
    value: JsonValue | dict[str, JsonValue] | None,
    *,
    source: str,
) -> dict[str, JsonValue]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{source} decisions must be a table or keyed flags")
    return {
        decision_id: _decision_value(decision_id, selected)
        for decision_id, selected in value.items()
    }


def _decision_value(decision_id: str, selected: JsonValue) -> JsonValue:
    if decision_id in _RESERVED_DECISIONS:
        raise ConfigError(
            f"decision {decision_id!r} is reserved; use its dedicated configuration carrier"
        )
    if _is_secret_like(decision_id):
        raise ConfigError(f"secret-like decision key is forbidden: {decision_id!r}")
    if isinstance(selected, str) and selected:
        return selected
    if isinstance(selected, list) and all(
        isinstance(item, str) and item for item in selected
    ):
        return list(selected)
    raise ConfigError(
        f"decision {decision_id!r} must be a string or string array"
    )


def _is_secret_like(value: str) -> bool:
    return any(marker in value.lower() for marker in _SECRET_KEY_MARKERS)
