import sys
import types

import pytest

from storage import LocalAttachmentStorage, build_attachment_storage


def test_local_storage_save_get_delete(tmp_path):
    s = LocalAttachmentStorage(tmp_path)
    s.save("CLM-1", "abc.png", b"hello", "image/png")
    assert s.exists("CLM-1", "abc.png")
    kind, value = s.get_file_or_url("CLM-1", "abc.png")
    assert kind == "path" and open(value, "rb").read() == b"hello"
    s.delete("CLM-1", "abc.png")
    assert not s.exists("CLM-1", "abc.png")
    s.delete("CLM-1", "abc.png")  # 멱등


def test_build_attachment_storage_defaults_to_local(tmp_path, monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    assert isinstance(build_attachment_storage(tmp_path), LocalAttachmentStorage)


@pytest.fixture
def fake_boto3(monkeypatch):
    calls = []

    class FakeS3Client:
        def put_object(self, **kw): calls.append(("put_object", kw))
        def generate_presigned_url(self, op, Params, ExpiresIn):
            calls.append(("presign", op))
            return f"https://fake-s3.example.com/{Params['Key']}?sig=xyz"
        def delete_object(self, **kw): calls.append(("delete_object", kw))
        def head_object(self, **kw): calls.append(("head_object", kw))

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda service, endpoint_url=None: FakeS3Client()))
    return calls


def test_build_attachment_storage_uses_s3_when_bucket_set(monkeypatch, fake_boto3, tmp_path):
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    storage = build_attachment_storage(tmp_path)
    storage.save("CLM-1", "abc.png", b"hello", "image/png")
    kind, url = storage.get_file_or_url("CLM-1", "abc.png")
    assert kind == "url" and "attachments/CLM-1/abc.png" in url
    storage.delete("CLM-1", "abc.png")
    assert [c[0] for c in fake_boto3] == ["put_object", "presign", "delete_object"]
