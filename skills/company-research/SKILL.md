---
name: company-research
description: Produce evidence-backed company research and risk analysis.
---
# Company Research

Use fetched official Home, About, Product, Catalog, Contact, Team, and News pages.
Separate observed facts from possible needs.  Every fact and personalization
point must cite a URL and excerpt.  If fetching fails return `fetch_failed`; if
no product/channel evidence exists return `no_evidence` with low confidence.
Never invent contacts.  Missing email or phone is `没有`.

