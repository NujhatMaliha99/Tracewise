"""Administrator-configured SMTP alerts; TLS is mandatory, secrets stay in memory."""

import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.errors import HeaderParseError
from email.message import EmailMessage
from email.utils import formatdate


def mailbox(value):
    if not isinstance(value, str) or not value.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError("Use a single ASCII mailbox address without a display name.")
    try:
        address = Address(addr_spec=value)
    except (ValueError, HeaderParseError) as exc:
        raise ValueError("Invalid mailbox address.") from exc
    if not address.username or not address.domain or address.addr_spec != value:
        raise ValueError("Use a complete mailbox address without a display name.")
    return value


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    sender: str = field(repr=False)
    recipient: str = field(repr=False)
    mode: str = "ssl"
    port: int = 465

    def __post_init__(self):
        if not isinstance(self.host, str) or len(self.host) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", self.host):
            raise ValueError("SMTP host must be an ASCII hostname, not a URL.")
        if (self.mode, self.port) not in {("ssl", 465), ("starttls", 587)}:
            raise ValueError("SMTP requires ssl on port 465 or starttls on port 587.")
        if not isinstance(self.username, str) or not self.username or any(c in self.username for c in "\r\n\x00"):
            raise ValueError("SMTP username is required.")
        if not isinstance(self.password, str) or not self.password or any(c in self.password for c in "\r\n\x00"):
            raise ValueError("SMTP password is required.")
        mailbox(self.sender)
        mailbox(self.recipient)

    @classmethod
    def from_env(cls, env):
        keys = {"HOST", "USERNAME", "PASSWORD", "FROM", "TO", "MODE", "PORT"}
        if not any("TRACEWISE_SMTP_" + key in env for key in keys):
            return None
        required = {"HOST", "USERNAME", "PASSWORD", "FROM", "TO"}
        if any(not env.get("TRACEWISE_SMTP_" + key) for key in required):
            raise ValueError("SMTP setup requires HOST, USERNAME, PASSWORD, FROM and TO environment variables.")
        mode = env.get("TRACEWISE_SMTP_MODE", "ssl")
        try:
            port = int(env.get("TRACEWISE_SMTP_PORT", "587" if mode == "starttls" else "465"))
        except ValueError:
            raise ValueError("SMTP port must be 465 or 587.") from None
        return cls(env["TRACEWISE_SMTP_HOST"], env["TRACEWISE_SMTP_USERNAME"],
                   env["TRACEWISE_SMTP_PASSWORD"], env["TRACEWISE_SMTP_FROM"],
                   env["TRACEWISE_SMTP_TO"], mode, port)


class EmailSender:
    def __init__(self, config):
        self.config = config

    def __call__(self, payload):
        config = self.config
        transition = payload["transition"]
        if transition not in {"opened", "recovered"}:
            raise ValueError("Unsupported email transition.")
        event_id = payload["event_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}:(opened|recovered)", event_id):
            raise ValueError("Invalid alert event ID.")
        message = EmailMessage()
        message["From"], message["To"] = config.sender, config.recipient
        message["Subject"] = "[Tracewise] " + ("API alert opened" if transition == "opened" else "API alert recovered")
        message["Date"] = formatdate(localtime=False, usegmt=True)
        message["Message-ID"] = f"<{event_id.replace(':', '.')}@tracewise.invalid>"
        message.set_content(f"{payload['text']}\n\nEvent ID: {event_id}\n\n"
                            "Recovery reflects successful health checks, not a verified root cause or final resolution.\n"
                            "This message contains a monitor summary, not raw logs.\n")
        context = ssl.create_default_context()
        if config.mode == "ssl":
            with smtplib.SMTP_SSL(config.host, config.port, timeout=5, context=context) as client:
                self._deliver(client, message)
        else:
            with smtplib.SMTP(config.host, config.port, timeout=5) as client:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
                self._deliver(client, message)

    def _deliver(self, client, message):
        client.login(self.config.username, self.config.password)
        refused = client.send_message(message, from_addr=self.config.sender, to_addrs=[self.config.recipient])
        if refused:
            raise smtplib.SMTPException("Recipient was not accepted.")


if __name__ == "__main__":
    import argparse
    import os
    import secrets
    parser = argparse.ArgumentParser(description="Send one explicitly requested synthetic test email using SMTP environment settings.")
    parser.add_argument("--test", action="store_true", required=True)
    parser.parse_args()
    try:
        config = SMTPConfig.from_env(os.environ)
        if config is None:
            raise ValueError("SMTP is not configured")
        EmailSender(config)({"transition": "opened", "event_id": "selftest-" + secrets.token_hex(6) + ":opened",
                             "text": "Tracewise synthetic test email. No API outage has been detected by this test."})
        print("Test email accepted by the SMTP server. Check inbox and spam; inbox delivery is not guaranteed.")
    except Exception as exc:
        print("Test email failed: " + type(exc).__name__ + ". Check SMTP settings and provider access; no secrets were printed.")
        raise SystemExit(1)
