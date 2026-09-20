from pathlib import Path
import zipfile

from src.artifact import (
    BUNDLE_INFO,
    build_release_bundle,
    extract_bundle_preview,
    sign_release_bundle,
    verify_release_bundle,
)
from src.recovery import create_deployment_manifest, sha256_file
from src.release import generate_keypair, sign_manifest


SOURCE_COMMIT = "a" * 40
RELEASE_ID = "phase12-test-release"


def _release_fixture(tmp_path: Path):
    root = tmp_path / "deploy"
    source = root / "src" / "engine.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")

    manifest = tmp_path / "release_manifest.json"
    release_signature = tmp_path / "release_signature.json"
    private_key = tmp_path / "offline" / "release-private.pem"
    public_key = tmp_path / "release-public.pem"
    bundle = tmp_path / "release.zip"
    bundle_signature = tmp_path / "release.zip.signature.json"

    generate_keypair(private_key, public_key)
    create_deployment_manifest(root, manifest, patterns=["src/**/*.py"])
    sign_manifest(manifest, private_key, release_signature)
    build_release_bundle(
        root,
        manifest,
        release_signature,
        public_key,
        bundle,
        source_commit=SOURCE_COMMIT,
        release_id=RELEASE_ID,
    )
    sign_release_bundle(bundle, private_key, bundle_signature)
    return root, source, manifest, release_signature, private_key, public_key, bundle, bundle_signature


def test_bundle_build_is_deterministic_for_same_signed_inputs(tmp_path):
    root, _source, manifest, release_signature, _private_key, public_key, first, _bundle_signature = _release_fixture(tmp_path)
    second = tmp_path / "release-second.zip"

    build_release_bundle(
        root,
        manifest,
        release_signature,
        public_key,
        second,
        source_commit=SOURCE_COMMIT,
        release_id=RELEASE_ID,
    )

    assert sha256_file(first) == sha256_file(second)


def test_signed_bundle_verifies_and_preview_extracts(tmp_path):
    _root, _source, _manifest, _release_signature, _private_key, public_key, bundle, bundle_signature = _release_fixture(tmp_path)

    result = verify_release_bundle(
        bundle,
        bundle_signature,
        public_key,
        expected_source_commit=SOURCE_COMMIT,
        expected_release_id=RELEASE_ID,
    )
    preview = tmp_path / "preview"
    extracted = extract_bundle_preview(
        bundle,
        bundle_signature,
        preview,
        public_key,
        expected_source_commit=SOURCE_COMMIT,
        expected_release_id=RELEASE_ID,
    )

    assert result["ok"] is True
    assert result["bundle_signature"]["code"] == "SIGNATURE_VALID"
    assert (preview / "src" / "engine.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert BUNDLE_INFO in extracted["extracted"]


def test_bundle_tampering_breaks_detached_signature(tmp_path):
    _root, _source, _manifest, _release_signature, _private_key, public_key, bundle, bundle_signature = _release_fixture(tmp_path)
    tampered = tmp_path / "tampered.zip"

    with zipfile.ZipFile(bundle, "r") as source_zip, zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as target_zip:
        for name in source_zip.namelist():
            payload = source_zip.read(name)
            if name == "src/engine.py":
                payload = b"VALUE = 2\n"
            target_zip.writestr(name, payload)

    result = verify_release_bundle(tampered, bundle_signature, public_key)

    assert result["ok"] is False
    assert result["issues"] == ["bundle_signature:BUNDLE_HASH_MISMATCH"]


def test_validly_signed_bundle_rejects_hidden_extra_member(tmp_path):
    _root, _source, _manifest, _release_signature, private_key, public_key, bundle, _bundle_signature = _release_fixture(tmp_path)
    extra = tmp_path / "extra.zip"
    extra_signature = tmp_path / "extra.signature.json"

    with zipfile.ZipFile(bundle, "r") as source_zip, zipfile.ZipFile(extra, "w", compression=zipfile.ZIP_DEFLATED) as target_zip:
        for name in source_zip.namelist():
            target_zip.writestr(name, source_zip.read(name))
        target_zip.writestr("unexpected.txt", b"hidden")
    sign_release_bundle(extra, private_key, extra_signature)

    result = verify_release_bundle(extra, extra_signature, public_key)

    assert result["ok"] is False
    assert "unexpected:unexpected.txt" in result["issues"]


def test_bundle_rejects_wrong_trust_key(tmp_path):
    _root, _source, _manifest, _release_signature, _private_key, _public_key, bundle, bundle_signature = _release_fixture(tmp_path)
    wrong_private = tmp_path / "wrong-private.pem"
    wrong_public = tmp_path / "wrong-public.pem"
    generate_keypair(wrong_private, wrong_public)

    result = verify_release_bundle(bundle, bundle_signature, wrong_public)

    assert result["ok"] is False
    assert result["issues"] == ["bundle_signature:PUBLIC_KEY_FINGERPRINT_MISMATCH"]


def test_anti_rollback_pins_commit_and_release_id(tmp_path):
    _root, _source, _manifest, _release_signature, _private_key, public_key, bundle, bundle_signature = _release_fixture(tmp_path)

    wrong_commit = verify_release_bundle(
        bundle,
        bundle_signature,
        public_key,
        expected_source_commit="b" * 40,
        expected_release_id=RELEASE_ID,
    )
    wrong_release = verify_release_bundle(
        bundle,
        bundle_signature,
        public_key,
        expected_source_commit=SOURCE_COMMIT,
        expected_release_id="older-release",
    )

    assert "anti_rollback:source_commit" in wrong_commit["issues"]
    assert "anti_rollback:release_id" in wrong_release["issues"]


def test_bundle_binds_to_deployed_manifest_and_release_signature(tmp_path):
    _root, _source, manifest, release_signature, _private_key, public_key, bundle, bundle_signature = _release_fixture(tmp_path)
    deployed_manifest = tmp_path / "deployed-manifest.json"
    deployed_release_signature = tmp_path / "deployed-release-signature.json"
    deployed_manifest.write_bytes(manifest.read_bytes())
    deployed_release_signature.write_bytes(release_signature.read_bytes())

    ok = verify_release_bundle(
        bundle,
        bundle_signature,
        public_key,
        deployed_manifest_path=deployed_manifest,
        deployed_release_signature_path=deployed_release_signature,
    )
    deployed_manifest.write_text(deployed_manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    mismatch = verify_release_bundle(
        bundle,
        bundle_signature,
        public_key,
        deployed_manifest_path=deployed_manifest,
        deployed_release_signature_path=deployed_release_signature,
    )

    assert ok["ok"] is True
    assert "deployed_binding:manifest" in mismatch["issues"]


def test_validly_signed_zip_slip_member_is_rejected(tmp_path):
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    generate_keypair(private_key, public_key)
    malicious = tmp_path / "malicious.zip"
    malicious_signature = tmp_path / "malicious.signature.json"
    with zipfile.ZipFile(malicious, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../escape.txt", b"nope")
    sign_release_bundle(malicious, private_key, malicious_signature)

    result = verify_release_bundle(malicious, malicious_signature, public_key)

    assert result["ok"] is False
    assert "unsafe bundle member" in result["issues"][0]
