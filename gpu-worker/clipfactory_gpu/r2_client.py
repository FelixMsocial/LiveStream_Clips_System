"""R2 (S3-compatible) client. Thin wrapper around boto3."""
from __future__ import annotations

import logging
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

log = logging.getLogger(__name__)


class R2Client:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
    ) -> None:
        self._bucket = bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=BotoConfig(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )

    def download(self, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        log.info("r2 download %s -> %s", key, dest)
        self._s3.download_file(self._bucket, key, str(dest))

    def upload(self, src: Path, key: str, content_type: str = "video/mp4") -> None:
        log.info("r2 upload %s -> %s", src, key)
        self._s3.upload_file(
            str(src),
            self._bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )

    def head(self, key: str) -> dict | None:
        try:
            return self._s3.head_object(Bucket=self._bucket, Key=key)
        except self._s3.exceptions.ClientError as e:  # type: ignore[attr-defined]
            if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise

    def presign_get(self, key: str, expires: int = 3600) -> str:
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires,
        )
