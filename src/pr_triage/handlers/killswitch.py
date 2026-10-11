"""AWS Budgets crossed the hard limit: switch AI reviews off. A person switches them back on."""

from __future__ import annotations

import logging
from typing import Any

from .. import aws
from ..env import PARAM_PREFIX

log = logging.getLogger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    aws.client("ssm").put_parameter(Name=f"{PARAM_PREFIX}/config/enabled", Value="off", Type="String", Overwrite=True)
    log.warning("budget alert received; AI reviews are now off")
    return {"enabled": "off"}
