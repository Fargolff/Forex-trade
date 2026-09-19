# Phase 11 — Cryptographically Signed Release Gate

Phase 11 adds an Ed25519 release-signing layer on top of the existing SHA-256 deployment manifest.

The goal is to answer two different questions before supervised live trading starts:

1. **Was this release manifest approved by the holder of the release-signing key?**
2. **Do the deployed files still match the approved manifest?**

The private signing key must remain outside the repository and outside the trading project directory. Only the public key is required on the trading machine.

## Security model

- `src.recovery` creates the deployment manifest and SHA-256 hashes of deployable files.
- `src.release` signs the manifest with an Ed25519 private key.
- `src.release` verifies the signature with the corresponding public key, then verifies the deployment file hashes.
- The Windows live supervisor can be configured to fail closed when signed-release verification fails.
- Verification is repeated before every supervisor start/restart.
- The existing live arming phrase and `live.enabled=true` checks are still required. Phase 11 does not bypass any Phase 7–10 safety gate.

## Files

Default release artifacts:

```text
release/release_manifest.json
release/release_signature.json
release/forex-release-public.pem
```

The private key should live somewhere separate, for example:

```text
D:\ForexReleaseKeys\forex-release-private.pem
```

Do **not** place the private key inside the repository, cloud-synced project folder, CI workspace, or trading runtime directory.

## 1. Generate the signing key pair

Prefer doing this once on a trusted/offline machine.

```powershell
$env:FOREX_RELEASE_PRIVATE_KEY = "D:\ForexReleaseKeys\forex-release-private.pem"
$env:FOREX_RELEASE_PUBLIC_KEY = "D:\ForexReleaseKeys\forex-release-public.pem"
python -m src.release --mode generate-keypair
```

`generate-keypair` refuses to overwrite an existing key.

Record the public-key fingerprint:

```powershell
python -m src.release --mode fingerprint --public-key "D:\ForexReleaseKeys\forex-release-public.pem"
```

Copy only the public key to the trading machine, normally as:

```text
release/forex-release-public.pem
```

## 2. Create the deployment manifest

On the exact release tree that will be deployed:

```powershell
python -m src.recovery --mode make-manifest --manifest release/release_manifest.json
```

The manifest covers the deployable Python code, requirements, example configs and Windows deployment scripts defined by `DEPLOYMENT_PATTERNS` in `src/recovery.py`.

## 3. Sign the manifest

The signing step should be performed only where the private key is available.

```powershell
python -m src.release `
  --mode sign `
  --manifest release/release_manifest.json `
  --signature release/release_signature.json `
  --private-key "D:\ForexReleaseKeys\forex-release-private.pem"
```

The signature document contains:

- algorithm (`Ed25519`)
- manifest SHA-256
- public-key fingerprint
- signing timestamp
- detached Base64 signature

## 4. Verify before deployment

On the trading machine:

```powershell
python -m src.release `
  --mode verify `
  --root . `
  --manifest release/release_manifest.json `
  --signature release/release_signature.json `
  --public-key release/forex-release-public.pem
```

Exit code `0` means the signature and all deployment hashes match. Verification failure exits non-zero (`3` for a normal verification failure).

Examples of failures detected:

- manifest changed after signing
- wrong public key
- invalid signature
- tracked deployment file missing
- tracked deployment file size changed
- tracked deployment file SHA-256 changed

## 5. Enable the production gate

Phase 11 is intentionally **opt-in** during migration so an existing Phase 9/10 deployment does not suddenly stop before the operator has installed a public key and signed artifacts.

Set this Windows user environment variable only after the release artifacts are installed:

```powershell
setx FOREX_REQUIRE_SIGNED_RELEASE "1"
```

Optional path overrides:

```powershell
setx FOREX_RELEASE_MANIFEST "release/release_manifest.json"
setx FOREX_RELEASE_SIGNATURE "release/release_signature.json"
setx FOREX_RELEASE_PUBLIC_KEY "release/forex-release-public.pem"
```

After `FOREX_REQUIRE_SIGNED_RELEASE=1`, `deploy/windows/run-live-supervisor.ps1` runs signed-release verification before every supervised-live start/restart. If verification fails, the script throws and **does not start the production live process**.

## Recommended operating procedure

```text
Merge reviewed code
      ↓
Run tests + operational soak
      ↓
Create deployment manifest from exact release tree
      ↓
Move manifest to trusted/offline signer
      ↓
Sign manifest with protected Ed25519 private key
      ↓
Deploy code + manifest + signature + public key
      ↓
Verify public-key fingerprint out-of-band
      ↓
Run src.release --mode verify
      ↓
Enable FOREX_REQUIRE_SIGNED_RELEASE=1
      ↓
Start Windows supervisor
      ↓
Supervisor re-verifies before every start/restart
```

## Key rotation

When rotating the signing key:

1. generate a new key pair on the trusted signing machine;
2. record and independently verify the new public-key fingerprint;
3. deploy the new public key to the trading machine;
4. create a fresh manifest from the intended release;
5. sign it with the new private key;
6. verify on the trading machine before restarting supervised live;
7. archive/revoke the old private key according to your own key-retention policy.

Never silently replace the public key on the trading machine without independently checking its fingerprint.

## Limitations

- Phase 11 proves release approval and detects deployment drift; it does not guarantee strategy profitability or broker correctness.
- A compromised trading machine with sufficient privileges can modify both code and the configured public key. Protect the Windows account, filesystem permissions and deployment process.
- The private key is intentionally not stored or managed by this repository.
- This implementation does not yet provide hardware-token/HSM signing or a remote transparency log.
