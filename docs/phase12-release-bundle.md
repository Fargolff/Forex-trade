# Phase 12 — Deterministic Signed Release Bundle & Provenance

Phase 12 packages an already verified Phase 11 release into a deterministic ZIP archive and signs the **entire bundle** with the protected Ed25519 release key.

This creates a stronger deployment chain:

```text
Trusted external public key
        ↓
Detached signature over the entire release ZIP
        ↓
Bundle provenance (source commit + release ID)
        ↓
Embedded Phase 11 signed deployment manifest
        ↓
Deployment file hashes
        ↓
Exact deployed manifest/signature binding
        ↓
Windows production supervisor
```

The private signing key remains off the trading machine.

## Why Phase 12 exists

Phase 11 proves that the deployment manifest was signed and that deployed files still match it. Phase 12 adds controls around the **artifact being transported and deployed**:

- deterministic archive generation;
- detached Ed25519 signature over the complete ZIP bytes;
- signed source-commit provenance;
- signed release identifier;
- strict member allowlist;
- duplicate-member rejection;
- path-traversal / ZIP-slip rejection;
- external trusted-public-key verification;
- anti-rollback pinning to an expected commit and release ID;
- binding between the bundle's embedded manifest/signature and the deployed manifest/signature;
- verify-before-extract preview flow.

## Default artifact names

```text
release/forex-release-bundle.zip
release/forex-release-bundle.signature.json
release/release_manifest.json
release/release_signature.json
release/forex-release-public.pem
```

The private key should remain outside the repository and outside the trading machine, for example:

```text
D:\ForexReleaseKeys\forex-release-private.pem
```

## 1. Build the Phase 11 signed release first

Create and sign the deployment manifest exactly as described in `docs/phase11-signed-release.md`.

```powershell
python -m src.recovery `
  --mode make-manifest `
  --manifest release/release_manifest.json

python -m src.release `
  --mode sign `
  --manifest release/release_manifest.json `
  --signature release/release_signature.json `
  --private-key "D:\ForexReleaseKeys\forex-release-private.pem"
```

Before Phase 12 packaging, the source tree must already pass Phase 11 verification.

## 2. Build the deterministic release bundle

Use the exact Git commit that corresponds to the release tree and a human-readable unique release ID.

```powershell
python -m src.artifact `
  --mode build `
  --root . `
  --manifest release/release_manifest.json `
  --release-signature release/release_signature.json `
  --public-key release/forex-release-public.pem `
  --archive release/forex-release-bundle.zip `
  --source-commit 0123456789abcdef0123456789abcdef01234567 `
  --release-id 2026-09-18-phase12-001
```

The builder first re-verifies the Phase 11 signed release. It refuses to package a deployment tree that already fails its signed manifest.

For the same signed inputs, source commit and release ID, the ZIP is generated with stable member ordering and timestamps so repeated builds produce the same archive SHA-256.

## 3. Sign the complete bundle on the trusted signing machine

```powershell
python -m src.artifact `
  --mode sign `
  --archive release/forex-release-bundle.zip `
  --bundle-signature release/forex-release-bundle.signature.json `
  --private-key "D:\ForexReleaseKeys\forex-release-private.pem"
```

The detached bundle signature covers the **entire ZIP byte-for-byte**, including:

- all deployable files;
- embedded Phase 11 manifest;
- embedded Phase 11 release signature;
- embedded public key copy;
- bundle metadata containing source commit and release ID.

## 4. Verify on the trading machine

Use the public key already trusted on the trading machine. Do not trust only the public key embedded inside the ZIP.

```powershell
python -m src.artifact `
  --mode verify `
  --archive release/forex-release-bundle.zip `
  --bundle-signature release/forex-release-bundle.signature.json `
  --public-key release/forex-release-public.pem `
  --expected-commit 0123456789abcdef0123456789abcdef01234567 `
  --expected-release-id 2026-09-18-phase12-001
```

Verification fails if any of the following occurs:

- ZIP bytes changed after signing;
- wrong public key is supplied;
- detached bundle signature is invalid;
- hidden/unexpected file exists in the archive;
- archive contains duplicate members;
- archive contains unsafe `..`/absolute/backslash paths;
- embedded public key does not match the external trusted key;
- embedded Phase 11 release signature is invalid;
- any deployment file differs from the embedded Phase 11 manifest;
- source commit or release ID differs from the operator's expected pin.

## 5. Preview extraction only

Phase 12 intentionally does not provide an automatic in-place production installer.

Verify and extract into an empty preview directory first:

```powershell
python -m src.artifact `
  --mode extract-preview `
  --archive release/forex-release-bundle.zip `
  --bundle-signature release/forex-release-bundle.signature.json `
  --public-key release/forex-release-public.pem `
  --expected-commit 0123456789abcdef0123456789abcdef01234567 `
  --expected-release-id 2026-09-18-phase12-001 `
  --target runtime/release-preview
```

Extraction occurs only after signed-bundle verification succeeds. The target must be empty.

## 6. Bind the archive to the deployed release

After the intended release files, Phase 11 manifest and Phase 11 release signature are present on the trading machine, verify that the archive metadata matches those deployed control files:

```powershell
python -m src.artifact `
  --mode verify `
  --archive release/forex-release-bundle.zip `
  --bundle-signature release/forex-release-bundle.signature.json `
  --public-key release/forex-release-public.pem `
  --expected-commit 0123456789abcdef0123456789abcdef01234567 `
  --expected-release-id 2026-09-18-phase12-001 `
  --deployed-manifest release/release_manifest.json `
  --deployed-release-signature release/release_signature.json
```

This prevents a valid signed bundle from being presented beside a different valid signed deployment.

## 7. Enable the Windows supervisor gate

Phase 12 remains opt-in during migration.

Phase 12 requires Phase 11 to be enabled as well:

```powershell
setx FOREX_REQUIRE_SIGNED_RELEASE "1"
setx FOREX_REQUIRE_SIGNED_BUNDLE "1"
setx FOREX_EXPECTED_RELEASE_COMMIT "0123456789abcdef0123456789abcdef01234567"
setx FOREX_EXPECTED_RELEASE_ID "2026-09-18-phase12-001"
```

Optional path overrides:

```powershell
setx FOREX_RELEASE_BUNDLE "release/forex-release-bundle.zip"
setx FOREX_RELEASE_BUNDLE_SIGNATURE "release/forex-release-bundle.signature.json"
setx FOREX_RELEASE_MANIFEST "release/release_manifest.json"
setx FOREX_RELEASE_SIGNATURE "release/release_signature.json"
setx FOREX_RELEASE_PUBLIC_KEY "release/forex-release-public.pem"
```

When `FOREX_REQUIRE_SIGNED_BUNDLE=1`:

1. the supervisor refuses to run unless Phase 11 signed-release verification is also enabled;
2. expected commit and release ID become mandatory;
3. the signed bundle is verified on every supervisor start/restart;
4. the bundle's embedded manifest/signature must match the deployed manifest/signature;
5. Phase 11 then verifies the deployed code against the signed manifest;
6. only after both gates pass can `src.production --mode supervised-live` start.

## Rollback procedure

A rollback should be explicit, not accidental.

To move to an older release:

1. choose the exact older signed bundle and its detached bundle signature;
2. verify the public-key fingerprint;
3. update `FOREX_EXPECTED_RELEASE_COMMIT` to the intended older commit;
4. update `FOREX_EXPECTED_RELEASE_ID` to the intended older release ID;
5. verify/extract the bundle to preview;
6. perform the operator-controlled deployment;
7. run Phase 12 verification with deployed binding;
8. run Phase 11 verification;
9. restart the supervisor only after both gates pass.

This means an old valid bundle cannot silently pass an environment that is pinned to a newer release.

## Limitations

- Phase 12 does not automatically copy files into the live production tree.
- The expected commit/release ID is an operator-controlled pin, not a remote transparency log.
- A machine administrator with sufficient privileges can replace code, environment variables and trusted keys; protect the Windows account and deployment permissions.
- The release bundle proves artifact integrity/provenance, not trading profitability or strategy correctness.
- Hardware-backed/HSM signing and remote release transparency remain future maturity work.
