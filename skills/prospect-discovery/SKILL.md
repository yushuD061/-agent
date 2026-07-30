---
name: prospect-discovery
description: Build compliant overseas prospect discovery plans from approved sources.
---
# Prospect Discovery

Use only uploaded lists, explicit URLs, and administrator-whitelisted public
sources.  If none exist, return `source_status: search_tasks_only`; search tasks
are not prospects.  Every candidate needs a source URL, evidence summary,
country, website, risk notes, and explicit email/phone results (`没有` when not
found).  Keep API secrets in environment variables and send fetched candidates
to company research before scoring.

