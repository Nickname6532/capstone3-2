"""클레임 첨부파일의 실제 바이트를 어디에 저장할지 추상화한다.

로컬 디스크(기본값, 개발/단일 서버용)와 S3 호환 오브젝트 스토리지(S3/Cloudflare
R2/Supabase Storage — S3_BUCKET 등 환경변수 설정 시) 둘 다 지원한다. Vercel 같은
서버리스 환경은 로컬 디스크가 인스턴스마다 휘발되므로, 운영 단계에서는 S3 연동이
필요하다(README의 "클레임 처리 상세" 절 참고).

DB(claims_store*.py)에는 메타데이터(파일명/저장명/용량/타입)만 남고, 이 모듈은
그 저장명(stored_name)을 열쇠 삼아 바이트를 어디서 읽고 쓸지만 책임진다.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class AttachmentStorage(ABC):
    @abstractmethod
    def save(self, claim_id: str, stored_name: str, content: bytes, content_type: str) -> None:
        """첨부파일 바이트를 저장한다."""

    @abstractmethod
    def get_file_or_url(self, claim_id: str, stored_name: str) -> tuple[str, str]:
        """("path", 로컬 절대경로) 또는 ("url", 다운로드용 외부 URL)을 반환한다.

        호출자(app.py)는 kind에 따라 FileResponse(path)를 쓰거나 그 URL로
        리다이렉트한다 — 저장소 종류가 바뀌어도 엔드포인트 코드는 바뀌지 않는다.
        """

    @abstractmethod
    def delete(self, claim_id: str, stored_name: str) -> None:
        """파일이 없어도 조용히 넘어간다(멱등)."""

    @abstractmethod
    def exists(self, claim_id: str, stored_name: str) -> bool: ...


class LocalAttachmentStorage(AttachmentStorage):
    """로컬 디스크 저장 — 기존 ATTACHMENTS_DIR 방식 그대로."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, claim_id: str, stored_name: str) -> Path:
        return self.base_dir / claim_id / stored_name

    def save(self, claim_id: str, stored_name: str, content: bytes, content_type: str) -> None:
        path = self._path(claim_id, stored_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get_file_or_url(self, claim_id: str, stored_name: str) -> tuple[str, str]:
        return "path", str(self._path(claim_id, stored_name))

    def delete(self, claim_id: str, stored_name: str) -> None:
        self._path(claim_id, stored_name).unlink(missing_ok=True)

    def exists(self, claim_id: str, stored_name: str) -> bool:
        return self._path(claim_id, stored_name).is_file()


class S3AttachmentStorage(AttachmentStorage):
    """S3/Cloudflare R2/Supabase Storage 등 S3 호환 API를 쓰는 오브젝트 스토리지.

    boto3는 선택 의존성이다 — S3_BUCKET 환경변수를 설정해 이 클래스를 실제로 쓸
    때만 import하므로, 로컬 디스크만 쓰는 환경에서는 설치하지 않아도 된다.
    자격증명(AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY)은 boto3가 환경변수에서
    알아서 읽는다 — 이 클래스가 직접 다루지 않는다.
    """

    def __init__(self, bucket: str, endpoint_url: Optional[str] = None, prefix: str = "attachments"):
        try:
            import boto3  # 선택 의존성: requirements.txt에는 주석으로만 안내한다
        except ImportError as e:
            raise RuntimeError(
                "S3_BUCKET이 설정되어 있지만 boto3가 설치되어 있지 않습니다. `pip install boto3`로 설치하세요."
            ) from e
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client("s3", endpoint_url=endpoint_url)

    def _key(self, claim_id: str, stored_name: str) -> str:
        return f"{self.prefix}/{claim_id}/{stored_name}"

    def save(self, claim_id: str, stored_name: str, content: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self.bucket, Key=self._key(claim_id, stored_name),
            Body=content, ContentType=content_type or "application/octet-stream",
        )

    def get_file_or_url(self, claim_id: str, stored_name: str) -> tuple[str, str]:
        url = self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": self._key(claim_id, stored_name)}, ExpiresIn=300,
        )
        return "url", url

    def delete(self, claim_id: str, stored_name: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._key(claim_id, stored_name))

    def exists(self, claim_id: str, stored_name: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(claim_id, stored_name))
            return True
        except Exception:
            return False


def build_attachment_storage(local_base_dir: Path) -> AttachmentStorage:
    """S3_BUCKET이 설정되어 있으면 S3 호환 스토리지를, 아니면 로컬 디스크를 쓴다."""
    bucket = os.environ.get("S3_BUCKET")
    if bucket:
        return S3AttachmentStorage(bucket, endpoint_url=os.environ.get("S3_ENDPOINT_URL"))
    return LocalAttachmentStorage(local_base_dir)
