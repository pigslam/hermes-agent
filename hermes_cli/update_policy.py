"""Update policy gates for the Reuben fork.

The package still exposes compatibility names such as ``hermes`` internally,
but this checkout is a forked Reuben Agent runtime. Passive checks against the
upstream Hermes project and browser-triggered self-updates stay disabled unless
an operator opts in deliberately.
"""

from __future__ import annotations

from typing import Any, Mapping

from utils import env_var_enabled, is_truthy_value


ENV_ENABLE_UPSTREAM_UPDATE_CHECKS = "REUBEN_ENABLE_UPSTREAM_UPDATE_CHECKS"
LEGACY_ENV_ENABLE_UPSTREAM_UPDATE_CHECKS = "HERMES_ENABLE_UPSTREAM_UPDATE_CHECKS"
ENV_ENABLE_DASHBOARD_SELF_UPDATE = "REUBEN_ENABLE_DASHBOARD_SELF_UPDATE"
LEGACY_ENV_ENABLE_DASHBOARD_SELF_UPDATE = "HERMES_ENABLE_DASHBOARD_SELF_UPDATE"


def _updates_config(config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    updates = config.get("updates", {}) if isinstance(config, Mapping) else {}
    return updates if isinstance(updates, Mapping) else {}


def passive_upstream_update_checks_enabled(
    config: Mapping[str, Any] | None = None,
) -> bool:
    """Return true when passive Hermes-upstream checks are explicitly enabled."""
    if env_var_enabled(ENV_ENABLE_UPSTREAM_UPDATE_CHECKS) or env_var_enabled(
        LEGACY_ENV_ENABLE_UPSTREAM_UPDATE_CHECKS
    ):
        return True
    return is_truthy_value(_updates_config(config).get("enable_upstream_checks"))


def dashboard_self_update_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Return true when dashboard/browser self-update actions may be offered."""
    if env_var_enabled(ENV_ENABLE_DASHBOARD_SELF_UPDATE) or env_var_enabled(
        LEGACY_ENV_ENABLE_DASHBOARD_SELF_UPDATE
    ):
        return True
    return is_truthy_value(_updates_config(config).get("enable_dashboard_self_update"))


def disabled_dashboard_update_message() -> str:
    return (
        "Dashboard self-update is disabled for this Reuben fork. Run "
        "`reuben update` from a terminal when you deliberately want to update, "
        "or set updates.enable_dashboard_self_update: true for this profile."
    )
