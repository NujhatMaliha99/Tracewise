import smtplib
import ssl
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from investigator.delivery import Delivery
from investigator.mail import EmailSender, SMTPConfig, mailbox
from investigator.monitoring import MonitorEngine
from investigator.outbound import EgressPolicy
from investigator.store import CaseStore
from investigator.stream import LiveBuffer
from server import InvestigatorServer


class MailTests(unittest.TestCase):
    def setUp(self):
        self.config = SMTPConfig("smtp.example.com", "sender@example.com", "test-only-secret", "sender@example.com", "receiver@example.com")
        self.payload = {"transition": "opened", "event_id": "alert1:opened", "text": "Tracewise: Synthetic API / opened / Unexpected HTTP 503"}

    def test_no_environment_means_disabled(self):
        self.assertIsNone(SMTPConfig.from_env({}))

    def test_partial_environment_fails_closed(self):
        with self.assertRaises(ValueError):
            SMTPConfig.from_env({"TRACEWISE_SMTP_HOST": "smtp.example.com"})

    def test_environment_defaults_and_starttls(self):
        env = {"TRACEWISE_SMTP_" + k: v for k, v in {"HOST": "smtp.example.com", "USERNAME": "user", "PASSWORD": "test-only", "FROM": "sender@example.com", "TO": "receiver@example.com"}.items()}
        self.assertEqual(SMTPConfig.from_env(env).port, 465)
        env["TRACEWISE_SMTP_MODE"] = "starttls"
        self.assertEqual(SMTPConfig.from_env(env).port, 587)
        env["TRACEWISE_SMTP_PORT"] = "invalid"
        with self.assertRaises(ValueError):
            SMTPConfig.from_env(env)

    def test_plaintext_and_mismatched_ports_rejected(self):
        for mode, port in [("plain", 25), ("ssl", 587), ("starttls", 465), ("ssl", 0)]:
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                SMTPConfig("smtp.example.com", "user", "password", "sender@example.com", "receiver@example.com", mode, port)

    def test_mailbox_header_injection_and_multiple_recipients_rejected(self):
        for value in ["x@example.com\r\nBcc: victim@example.com", "x@example.com,y@example.com", "Name <x@example.com>", "missing-domain", "x@", " x@example.com", "x@example.com\x00"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                mailbox(value)

    def test_secret_configuration_not_in_repr(self):
        representation = repr(self.config)
        self.assertNotIn(self.config.password, representation)
        self.assertNotIn(self.config.username, representation)
        self.assertNotIn(self.config.recipient, representation)

    def test_invalid_host_rejected(self):
        for host in ["https://smtp.example.com", "smtp.example.com\n", "smtp.example.com:465", ""]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                SMTPConfig(host, "user", "password", "sender@example.com", "receiver@example.com")

    def test_ssl_message_and_certificate_verification(self):
        with patch("investigator.mail.smtplib.SMTP_SSL") as factory:
            client = factory.return_value.__enter__.return_value
            client.send_message.return_value = {}
            EmailSender(self.config)(self.payload)
            context = factory.call_args.kwargs["context"]
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            client.login.assert_called_once_with(self.config.username, self.config.password)
            message = client.send_message.call_args.args[0]
            self.assertEqual(str(message["To"]), self.config.recipient)
            self.assertIn("alert opened", str(message["Subject"]))
            self.assertEqual(message["Message-ID"], "<alert1.opened@tracewise.invalid>")
            self.assertIn(self.payload["text"], message.get_content())
            self.assertNotIn(self.config.password, message.as_string())

    def test_starttls_before_login(self):
        config = SMTPConfig("smtp.example.com", "user", "password", "sender@example.com", "receiver@example.com", "starttls", 587)
        with patch("investigator.mail.smtplib.SMTP") as factory:
            client = factory.return_value.__enter__.return_value
            client.send_message.return_value = {}
            EmailSender(config)(self.payload)
            self.assertEqual([call[0] for call in client.method_calls], ["ehlo", "starttls", "ehlo", "login", "send_message"])
            self.assertTrue(client.starttls.call_args.kwargs["context"].check_hostname)

    def test_tls_failure_never_logs_in(self):
        config = SMTPConfig("smtp.example.com", "user", "password", "sender@example.com", "receiver@example.com", "starttls", 587)
        with patch("investigator.mail.smtplib.SMTP") as factory:
            client = factory.return_value.__enter__.return_value
            client.starttls.side_effect = smtplib.SMTPNotSupportedError("No TLS")
            with self.assertRaises(smtplib.SMTPNotSupportedError):
                EmailSender(config)(self.payload)
            client.login.assert_not_called()
            client.send_message.assert_not_called()

    def test_refused_recipient_is_failure(self):
        with patch("investigator.mail.smtplib.SMTP_SSL") as factory:
            factory.return_value.__enter__.return_value.send_message.return_value = {self.config.recipient: (550, b"rejected")}
            with self.assertRaises(smtplib.SMTPException):
                EmailSender(self.config)(self.payload)

    def test_recovery_subject(self):
        with patch("investigator.mail.smtplib.SMTP_SSL") as factory:
            client = factory.return_value.__enter__.return_value
            client.send_message.return_value = {}
            EmailSender(self.config)(self.payload | {"transition": "recovered", "event_id": "alert1:recovered"})
            self.assertIn("recovered", str(client.send_message.call_args.args[0]["Subject"]))

    def test_event_header_injection_rejected(self):
        with patch("investigator.mail.smtplib.SMTP_SSL") as factory:
            with self.assertRaises(ValueError):
                EmailSender(self.config)(self.payload | {"event_id": "x\r\nBcc: victim@example.com"})
            factory.assert_not_called()

    def test_email_uses_durable_retry_queue_without_exposing_credentials(self):
        with closing(CaseStore()) as store:
            sender = Mock(side_effect=smtplib.SMTPAuthenticationError(535, b"test-only-secret"))
            clock = [0]
            delivery = Delivery(store, EgressPolicy(), email_sender=sender, clock=lambda: clock[0])
            with store.lock, store.db:
                delivery.enqueue("alert1", "opened", "Synthetic API", "Unexpected HTTP 503")
            delivery.flush()
            status = delivery.status()
            self.assertEqual(status["channel"], "email")
            self.assertEqual(status["counts"], {"pending": 1})
            self.assertEqual(status["recent"][0]["last_error"], "SMTPAuthenticationError")
            self.assertNotIn("test-only-secret", str(status))
            clock[0] = 15
            sender.side_effect = None
            delivery.flush()
            self.assertEqual(delivery.status()["counts"], {"sent": 1})

    def test_channel_conflict_rejected_before_binding(self):
        with self.assertRaises(ValueError):
            InvestigatorServer(("127.0.0.1", 0), smtp_config=self.config, webhook_url="https://hooks.example.com/secret")

    def test_monitor_transitions_enqueue_email(self):
        with closing(CaseStore()) as store:
            sender = Mock()
            delivery = Delivery(store, EgressPolicy(), email_sender=sender)
            ok = [False]
            def result(_):
                return {"timestamp": "2026-10-14T00:00:00Z", "status": 200 if ok[0] else 503, "latency": 1, "success": ok[0], "reason": "Expected status" if ok[0] else "Unexpected HTTP 503"}
            engine = MonitorEngine(store, LiveBuffer(), probe_fn=result, delivery=delivery)
            monitor = engine.create({"name": "Synthetic API", "url": "http://127.0.0.1:8000/", "failure_threshold": 1, "recovery_threshold": 1}, True)
            engine.check(monitor["id"])
            engine.check(monitor["id"])
            ok[0] = True
            engine.check(monitor["id"])
            self.assertEqual(delivery.status()["counts"], {"pending": 2})
            delivery.flush()
            delivery.flush()
            self.assertEqual([call.args[0]["transition"] for call in sender.call_args_list], ["opened", "recovered"])


if __name__ == "__main__":
    unittest.main()
