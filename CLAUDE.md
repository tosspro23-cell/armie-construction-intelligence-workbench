# Contributor guidance

Read `PROJECT_STATE.md`, `AGENT_HANDOFF.md`, `README.md`, and `docs/architecture.md` before changing this repository.

- Preserve typed planning, capability gates, evidence, independent verification, and honest failure dispositions.
- Use only the synthetic public fixtures in `demo_data/`; never commit private data, runtime traces, secrets, or machine-specific paths.
- Before a change, add or update a focused test and run `PYTHONPATH=apps/api python3 -m pytest -q` plus `(cd apps/web && npm run build)`.
- Keep product changes scoped and document release-claim changes. Do not claim a provider, deployment, or evaluation suite is validated without current evidence.
- Work on a branch and leave merge/release decisions to the repository owner.
