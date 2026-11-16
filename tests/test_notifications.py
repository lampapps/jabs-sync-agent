import smtplib
from types import SimpleNamespace

import requests

from app import kuma, mailer
from app.config import EmailConfig


def email(**kwargs) -> EmailConfig:
    return EmailConfig(smtp_server="smtp.example.com", to_addrs=["a@example.com"], from_addr="me@example.com", **kwargs)


class FakeSMTP:
    instances = []

    def __init__(self, server, port, timeout=None, context=None):
        self.server, self.port, self.started_tls, self.logged_in, self.sent = server, port, False, None, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent.append(message)


class TestMailer:
    def test_it_needs_a_server_and_recipients(self):
        assert mailer.is_configured(EmailConfig()) is False
        assert mailer.send(EmailConfig(), "s", "b") is False
        assert mailer.is_configured(email()) is True

    def test_a_message_is_sent_with_starttls_and_the_login(self, monkeypatch):
        FakeSMTP.instances.clear()
        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        monkeypatch.setenv("JABS_SMTP_USER", "user")
        monkeypatch.setenv("JABS_SMTP_PASS", "pw")
        assert mailer.send(email(), "Subject", "Body") is True
        server = FakeSMTP.instances[0]
        assert server.started_tls and server.logged_in == ("user", "pw")
        assert server.sent[0]["Subject"] == "Subject" and server.sent[0]["To"] == "a@example.com"

    def test_development_mode_marks_the_subject(self, monkeypatch):
        FakeSMTP.instances.clear()
        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        monkeypatch.setenv("ENV_MODE", "development")
        mailer.send(email(), "Subject", "Body")
        assert FakeSMTP.instances[0].sent[0]["Subject"] == "[DEV] Subject"

    def test_a_failure_is_reported_as_false(self, monkeypatch):
        def broken(*args, **kwargs):
            raise OSError("no route")
        monkeypatch.setattr(smtplib, "SMTP", broken)
        assert mailer.send(email(), "s", "b") is False

    def test_notify_follows_the_notify_on_settings(self, monkeypatch):
        sent = []
        monkeypatch.setattr(mailer, "send", lambda cfg, subject, body: sent.append(subject) or True)
        assert mailer.notify(email(), "error", "failed", "b") is True
        assert mailer.notify(email(), "backup_complete", "done", "b") is False
        assert sent == ["failed"]


class TestKuma:
    def test_nothing_happens_without_a_url_or_on_a_dry_run(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
        assert kuma.ping("up") is False
        monkeypatch.setenv("UPTIME_KUMA_URL", "http://kuma/api/push/abc")
        assert kuma.ping("up", dry_run=True) is False

    def test_a_ping_carries_status_and_message(self, monkeypatch):
        urls = []
        monkeypatch.setenv("UPTIME_KUMA_URL", "http://kuma/api/push/abc?status=up&msg=OK&ping=")
        monkeypatch.setattr(requests, "get", lambda url, timeout=None: urls.append(url) or SimpleNamespace(status_code=200, text=""))
        assert kuma.ping("down", "job failed") is True
        assert urls == ["http://kuma/api/push/abc?status=down&msg=job%20failed&ping="]

    def test_a_failed_ping_returns_false(self, monkeypatch):
        monkeypatch.setenv("UPTIME_KUMA_URL", "http://kuma/api/push/abc")

        def broken(*args, **kwargs):
            raise requests.exceptions.ConnectionError("down")
        monkeypatch.setattr(requests, "get", broken)
        assert kuma.ping("up") is False
