from __future__ import annotations

from functools import cache
from typing import Any

from .env import required


@cache
def client(service: str) -> Any:
    import boto3

    return boto3.client(service)


@cache
def table() -> Any:
    import boto3

    return boto3.resource("dynamodb").Table(required("TABLE_NAME"))


def error_code(err: Exception) -> str:
    """The AWS error code of a botocore ClientError, or an empty string."""
    response = getattr(err, "response", None) or {}
    return str(response.get("Error", {}).get("Code", ""))
