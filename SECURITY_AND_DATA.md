# Security and Data Policy

This repository is designed for public reference use.

- No original private, customer, recruiter, or proprietary project files are included.
- `demo_data/` contains synthetic IFC and PDF fixtures generated locally by `scripts/generate_demo_data.py`, plus two real, openly-licensed buildings (`demo_data/projects/duplex/`, `demo_data/projects/digitalhub/`) with a full attribution record in each project's own `ATTRIBUTION.md` -- not proprietary or customer data, and not a claim of authorship.
- Runtime traces, evidence crops, screenshots, model caches, and local databases are ignored.
- `.env` files and credentials are excluded; use `.env.example` as a template.
- Contributors must not add IFC/PDF files unless they own the rights and the files are clearly redistributable.
- New demo assets should be synthetic or accompanied by explicit licensing and attribution.
- Keep secrets and absolute machine paths out of source, tests, docs, fixtures, and screenshots.

The application is a local reference workbench, not a security boundary or a production data-governance service.
