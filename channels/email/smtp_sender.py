"""SMTP_SSL adapter. It is never registered as an Agent tool."""

from __future__ import annotations

import smtplib
import socket
import ssl
from email.message import EmailMessage

from agent.business.email_secret_store import EmailSecretStore, EmailSecretStoreError
from channels.email.admin_contracts import EMAIL_PROVIDER_PRESETS


class SmtpDeliveryError(RuntimeError):
    def __init__(self, code: str, *, permanent: bool = False, outcome_unknown: bool = False):
        self.code = code
        self.permanent = permanent
        self.outcome_unknown = outcome_unknown
        super().__init__(code)


class SmtpSslSender:
    def __init__(self, secret_store: EmailSecretStore, *, timeout_seconds: int = 20,
                 smtp_factory=smtplib.SMTP_SSL):
        self.secret_store = secret_store
        self.timeout_seconds = timeout_seconds
        self.smtp_factory = smtp_factory
        self._presets = {item.provider.value: item for item in EMAIL_PROVIDER_PRESETS}

    def send(self, delivery: dict, account: dict) -> str | None:
        preset = self._presets[account["provider"]]
        message = EmailMessage()
        message["From"] = f'{account["sender_name"]} <{account["address"]}>'
        message["To"] = delivery["recipient"]
        message["Subject"] = delivery["subject_snapshot"]
        message["Message-ID"] = delivery["smtp_message_id"]
        if delivery.get("in_reply_to"):
            message["In-Reply-To"] = delivery["in_reply_to"]
            message["References"] = delivery["in_reply_to"]
        message.set_content(delivery["body_snapshot"])
        client = None
        try:
            auth_code = self.secret_store.get(account["secret_ref"])
            client = self.smtp_factory(
                preset.smtp.host, preset.smtp.port, timeout=self.timeout_seconds,
                context=ssl.create_default_context(),
            )
            client.login(account["address"], auth_code)
            refused = client.send_message(message)
            if refused:
                raise SmtpDeliveryError("email_smtp_recipient_refused", permanent=True)
            return message["Message-ID"]
        except SmtpDeliveryError:
            raise
        except smtplib.SMTPAuthenticationError as exc:
            raise SmtpDeliveryError("email_smtp_authentication_failed", permanent=True) from exc
        except EmailSecretStoreError as exc:
            raise SmtpDeliveryError("email_smtp_credential_unavailable", permanent=True) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise SmtpDeliveryError("email_smtp_recipient_refused", permanent=True) from exc
        except smtplib.SMTPDataError as exc:
            permanent = 500 <= int(exc.smtp_code) < 600
            raise SmtpDeliveryError("email_smtp_data_rejected", permanent=permanent) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise SmtpDeliveryError("email_smtp_outcome_unknown", outcome_unknown=True) from exc
        except (ssl.SSLError, OSError, smtplib.SMTPException) as exc:
            raise SmtpDeliveryError("email_smtp_temporary_failure") from exc
        finally:
            if client is not None:
                try:
                    client.quit()
                except (OSError, smtplib.SMTPException):
                    try:
                        client.close()
                    except OSError:
                        pass
