---
name: prospect-list-enrichment
description: Clean, normalize, deduplicate, and prepare uploaded prospect lists.
---
# Prospect List Enrichment

Never modify the original upload or generate companies.  Normalize by domain,
then company name and country.  Preserve all source URLs and merge useful source
notes.  Rows without company name or website need review; out-of-market rows
are excluded with a reason.  Research must run before scoring or email drafting.

