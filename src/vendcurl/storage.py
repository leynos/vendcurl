from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ObjectStoreError(RuntimeError):
    """Raised for object-store failures."""


@dataclass(frozen=True)
class S3Config:
    bucket: str
    prefix: str = "vendcurl"
    endpoint_url: str | None = None
    region_name: str | None = None
    profile_name: str | None = None


class S3Store:
    """Small boto3 wrapper that keeps imports lazy for testability."""

    def __init__(self, config: S3Config):
        self.config = config
        self._client = None
        self._client_error_type = None

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.exceptions import ClientError
            except ImportError as exc:  # pragma: no cover - exercised only in broken installs
                raise ObjectStoreError(
                    "boto3 is required for S3 access; install vendcurl with its dependencies"
                ) from exc

            session_kwargs: dict[str, str] = {}
            if self.config.profile_name:
                session_kwargs["profile_name"] = self.config.profile_name
            session = boto3.Session(**session_kwargs)

            client_kwargs: dict[str, str] = {}
            if self.config.endpoint_url:
                client_kwargs["endpoint_url"] = self.config.endpoint_url
            if self.config.region_name:
                client_kwargs["region_name"] = self.config.region_name

            self._client = session.client("s3", **client_kwargs)
            self._client_error_type = ClientError
        return self._client

    def get_json(self, key: str) -> dict[str, Any] | None:
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        except Exception as exc:
            if self._is_not_found(exc):
                return None
            raise ObjectStoreError(f"failed to read s3://{self.config.bucket}/{key}: {exc}") from exc

        body = response["Body"].read()
        try:
            decoded = body.decode("utf-8")
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ObjectStoreError(f"manifest is not valid JSON: s3://{self.config.bucket}/{key}") from exc
        if not isinstance(value, dict):
            raise ObjectStoreError(f"manifest is not a JSON object: s3://{self.config.bucket}/{key}")
        return value

    def put_json(self, key: str, value: dict[str, Any]) -> None:
        body = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        try:
            self.client.put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=body,
                ContentType="application/json; charset=utf-8",
            )
        except Exception as exc:
            raise ObjectStoreError(f"failed to write s3://{self.config.bucket}/{key}: {exc}") from exc

    def download_file(self, key: str, target: Path) -> bool:
        try:
            self.client.download_file(self.config.bucket, key, str(target))
            return True
        except Exception as exc:
            if self._is_not_found(exc):
                return False
            raise ObjectStoreError(f"failed to download s3://{self.config.bucket}/{key}: {exc}") from exc

    def object_exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.config.bucket, Key=key)
            return True
        except Exception as exc:
            if self._is_not_found(exc):
                return False
            raise ObjectStoreError(f"failed to stat s3://{self.config.bucket}/{key}: {exc}") from exc

    def upload_file_if_missing(self, source: Path, key: str, *, metadata: dict[str, str] | None = None) -> bool:
        if self.object_exists(key):
            return False
        extra_args: dict[str, Any] = {}
        if metadata:
            extra_args["Metadata"] = metadata
        try:
            self.client.upload_file(str(source), self.config.bucket, key, ExtraArgs=extra_args)
        except Exception as exc:
            raise ObjectStoreError(f"failed to upload s3://{self.config.bucket}/{key}: {exc}") from exc
        return True

    def _is_not_found(self, exc: BaseException) -> bool:
        client_error_type = self._client_error_type
        if client_error_type is not None and isinstance(exc, client_error_type):
            error = getattr(exc, "response", {}).get("Error", {})
            code = str(error.get("Code", ""))
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            return code in {"404", "NoSuchKey", "NotFound"} or status == 404
        return False


def join_key(*parts: str | None) -> str:
    cleaned = [part.strip("/") for part in parts if part and part.strip("/")]
    return "/".join(cleaned)
