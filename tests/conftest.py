import pytest


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """Tests must never talk to Telegram or Anthropic with the real keys from .env."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
