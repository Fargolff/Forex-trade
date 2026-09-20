from __future__ import annotations

import base64
import hashlib
import io
import tarfile
from pathlib import Path

EXPECTED_LENGTH = 15828
EXPECTED_SHA256 = "691b383d40978048011be324b7a8bfc907cc6950d07bfdd882d02d86b6326438"
PARTS = [Path(f"tools/phase35_payload/part{i:02d}.txt") for i in range(1, 6)]

missing = [str(path) for path in PARTS if not path.is_file()]
if missing:
    raise RuntimeError(f"Phase 35 payload parts missing: {missing}")

payload = "".join(path.read_text(encoding="utf-8").strip() for path in PARTS)
digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
if len(payload) != EXPECTED_LENGTH or digest != EXPECTED_SHA256:
    raise RuntimeError(
        "Phase 35 payload transport checksum mismatch: "
        f"length={len(payload)} expected_length={EXPECTED_LENGTH} "
        f"sha256={digest} expected_sha256={EXPECTED_SHA256}"
    )

archive = base64.b64decode(payload, validate=True)
root = Path(".").resolve()
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
    for member in tf.getmembers():
        if member.islnk() or member.issym():
            raise RuntimeError(f"Phase 35 payload links are not allowed: {member.name}")
        target = (root / member.name).resolve()
        if target == root or root not in target.parents:
            raise RuntimeError(f"Phase 35 payload path escapes repository root: {member.name}")
    tf.extractall(root)

print(f"Phase 35 payload verified and extracted: length={len(payload)} sha256={digest}")
