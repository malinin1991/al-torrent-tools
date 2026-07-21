from app.logging_filters import redact_secrets, setup_redacted_logging


def test_redact_telegram_bot_token_in_url() -> None:
    token = "8180462006:AAFTzVBg5Y5UXa9lITx0LNjV_J9zK2kRXHU"
    url = f"HTTP Request: POST https://api.telegram.org/bot{token}/getUpdates \"HTTP/1.1 200 OK\""
    redacted = redact_secrets(url)
    assert token not in redacted
    assert "api.telegram.org/bot***/getUpdates" in redacted


def test_redact_token_in_exception_text() -> None:
    token = "8180462006:AAFTzVBg5Y5UXa9lITx0LNjV_J9zK2kRXHU"
    msg = f"The token `{token}` was rejected by the server."
    assert token not in redact_secrets(msg)
    assert "***" in redact_secrets(msg)


def test_setup_redacted_logging_installs_without_error() -> None:
    setup_redacted_logging()
