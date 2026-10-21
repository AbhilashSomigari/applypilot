# uv marks .venv hidden on macOS and Python 3.12.13 skips hidden .pth files,
# so every target runs with PYTHONPATH=src instead of relying on the editable install.
export PYTHONPATH := src

.PHONY: test lint poll run stats jobs seed

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

poll:
	uv run applypilot poll

run:
	uv run applypilot run

stats:
	uv run applypilot stats

jobs:
	uv run applypilot jobs --fresh --hours 48

seed:
	uv run applypilot seed curated
	uv run applypilot seed yc
	uv run applypilot seed h1b
