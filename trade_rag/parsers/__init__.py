"""Bounded document parsing adapters. Persistence is owned by later ingestion stages."""

from .pdf import PdfProcessingError, parse_document_bytes, parse_pdf_bytes

__all__ = ["PdfProcessingError", "parse_document_bytes", "parse_pdf_bytes"]
