from pathlib import Path

from src.recovery import create_deployment_manifest
from src.release import generate_keypair, sign_manifest, verify_release


def _signed_fixture(tmp_path: Path):
    root = tmp_path / "deploy"
    source = root / "src" / "engine.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")

    manifest = tmp_path / "release_manifest.json"
    signature = tmp_path / "release_signature.json"
    private_key = tmp_path / "offline" / "release-private.pem"
    public_key = tmp_path / "release-public.pem"

    generate_keypair(private_key, public_key)
    create_deployment_manifest(root, manifest, patterns=["src/**/*.py"])
    sign_manifest(manifest, private_key, signature)
    return root, source, manifest, signature, private_key, public_key


def test_signed_release_verifies(tmp_path):
    root, _source, manifest, signature, _private_key, public_key = _signed_fixture(tmp_path)

    result = verify_release(root, manifest, signature, public_key)

    assert result["ok"] is True
    assert result["signature"]["code"] == "SIGNATURE_VALID"
    assert result["deployment"]["ok"] is True


def test_signed_release_detects_code_drift(tmp_path):
    root, source, manifest, signature, _private_key, public_key = _signed_fixture(tmp_path)
    source.write_text("VALUE = 2\n", encoding="utf-8")

    result = verify_release(root, manifest, signature, public_key)

    assert result["ok"] is False
    assert result["signature"]["ok"] is True
    assert result["deployment"]["issues"] == ["sha256:src/engine.py"]


def test_signed_release_detects_manifest_tampering(tmp_path):
    root, _source, manifest, signature, _private_key, public_key = _signed_fixture(tmp_path)
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    result = verify_release(root, manifest, signature, public_key)

    assert result["ok"] is False
    assert result["signature"]["code"] == "MANIFEST_HASH_MISMATCH"
    assert result["deployment"] is None


def test_signed_release_rejects_wrong_public_key(tmp_path):
    root, _source, manifest, signature, _private_key, _public_key = _signed_fixture(tmp_path)
    wrong_private = tmp_path / "wrong-private.pem"
    wrong_public = tmp_path / "wrong-public.pem"
    generate_keypair(wrong_private, wrong_public)

    result = verify_release(root, manifest, signature, wrong_public)

    assert result["ok"] is False
    assert result["signature"]["code"] == "PUBLIC_KEY_FINGERPRINT_MISMATCH"


def test_key_generation_refuses_overwrite(tmp_path):
    private_key = tmp_path / "release-private.pem"
    public_key = tmp_path / "release-public.pem"
    generate_keypair(private_key, public_key)

    try:
        generate_keypair(private_key, public_key)
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing release keys must never be overwritten")
