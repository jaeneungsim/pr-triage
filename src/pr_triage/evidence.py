"""Raw evidence in S3: the collected context, the AI answer and the check results of each run."""

from __future__ import annotations

import json
from typing import Any

from . import aws
from .env import required


def run_prefix(repo: str, pr: int, sha: str, execution: str) -> str:
    return f"runs/{repo}/{pr}/{sha}/{execution}"


def put_json(key: str, value: Any) -> str:
    aws.client("s3").put_object(
        Bucket=required("EVIDENCE_BUCKET"),
        Key=key,
        Body=json.dumps(value, ensure_ascii=False, default=str).encode(),
        ContentType="application/json",
    )
    return key


def get_json(key: str) -> Any:
    obj = aws.client("s3").get_object(Bucket=required("EVIDENCE_BUCKET"), Key=key)
    return json.loads(obj["Body"].read())
