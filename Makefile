install:
	python3 -m pip install -e 'apps/api[dev]'

dev:
	./scripts/dev.sh

test:
	PYTHONPATH=apps/api python3 -m pytest -q

build-web:
	cd apps/web && npm run build

generate-demo:
	python3 scripts/generate_demo_data.py
