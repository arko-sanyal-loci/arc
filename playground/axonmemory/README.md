# Axon Memory and Billo shared sections

`shared-sections.html` is the source for the publication previews and GitHub map
shown on both sites. Edit it first, then run:

```bash
python3 playground/axonmemory/scripts/sync_sections.py
python3 playground/axonmemory/scripts/sync_sections.py --check
```

Commit both updated pages and the shared source together. Publishing still
requires deploying each site's page to its own host and verifying each live
result; this script does not deploy or fetch current repository metadata.
Review public account and publication links before each publication update.
