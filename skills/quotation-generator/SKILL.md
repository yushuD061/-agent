---
name: quotation-generator
description: Generate deterministic, pending-confirmation quotation drafts and files.
---
# Quotation Generator

Match exact SKU and an approved, versioned price tier.  Require buyer, quantity,
currency, incoterm, payment terms, validity, product size, packing size, MOQ, and
lead time.  Block below-MOQ requests without sample approval and CIF/DDP without
confirmed freight/insurance.  Save authoritative JSON before HTML/Excel export.
Every output is labelled `待确认 / DRAFT`, has `human_review_required: true`, and
cannot be published or sent by this skill.

