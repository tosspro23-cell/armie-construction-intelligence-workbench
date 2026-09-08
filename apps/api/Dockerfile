FROM python:3.11-slim

WORKDIR /app
COPY apps/api/pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir .
COPY apps/api /app
# Synthetic, public-safe fixture data (SECURITY_AND_DATA.md) -- bundled so a
# cloud deployment can answer real demo questions out of the box, matching
# the local dev experience where the same files are served from the repo
# checkout. Settings.data_dir defaults to "./demo_data", resolved against
# this WORKDIR.
COPY demo_data /app/demo_data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

