from pathlib import Path

path = Path('src/production.py')
text = path.read_text(encoding='utf-8')
text = text.replace(
    'from .paper import load_portfolio_bundle\nfrom .runtime_liveness import REQUIRE_ENV as RUNTIME_LIVENESS_REQUIRE_ENV, append_liveness_from_env\n',
    'from .paper import load_portfolio_bundle\n\nRUNTIME_LIVENESS_REQUIRE_ENV = "FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER"\n',
)
old = '''    try:\n        return append_liveness_from_env(\n            Path.cwd(),\n            stage=stage,\n            cycle=cycle,\n            health=health,\n        )\n'''
new = '''    try:\n        # Lazy import avoids the existing recovery -> production dependency\n        # from becoming a production -> liveness -> recovery cycle.\n        from .runtime_liveness import append_liveness_from_env\n\n        return append_liveness_from_env(\n            Path.cwd(),\n            stage=stage,\n            cycle=cycle,\n            health=health,\n        )\n'''
if old not in text and new not in text:
    raise RuntimeError('Phase 33 publish marker not found')
text = text.replace(old, new, 1)
path.write_text(text, encoding='utf-8')
