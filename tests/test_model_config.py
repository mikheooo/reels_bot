"""Deterministic checks for the GEMINI_MODEL contract.

- Default model is baked into Settings (no .env entry required).
- A GEMINI_MODEL env value overrides the default (this is the
  documented switch-models-without-code-changes mechanism).

No network, no credentials: both cases use only Settings parsing.
"""

from app.core.config import Settings


def test_gemini_model_default(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert Settings().gemini_model == "gemini-3.7-flash"


def test_gemini_model_env_override(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-9.9-test")
    assert Settings().gemini_model == "gemini-9.9-test"
