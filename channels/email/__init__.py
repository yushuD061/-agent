"""Read-only email ingestion adapters; this package never sends email."""

from .contracts import AttachmentMeta, EmailEnvelope, EmailSource
from .mime_parser import EmailParseLimits, EmailSecurityError, parse_email

__all__ = ["AttachmentMeta", "EmailEnvelope", "EmailSource", "EmailParseLimits", "EmailSecurityError", "parse_email"]
