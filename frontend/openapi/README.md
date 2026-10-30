# Backend OpenAPI snapshot (checked in).

#

# This file is the contract Orval generates the frontend API client

# from (`npm run api:gen` -> `src/api/generated/`). Regenerate it after

# backend route or schema changes with a live backend:

#

# uv run uvicorn openlens.main:app &

# curl -s http://127.0.0.1:8000/openapi.json | python -m json.tool > frontend/openapi/openapi.json

#

# Or offline (routes only, no engine needed):

#

# OPENLENS_CONFIG=configs/default.yaml uv run python -c \

# "import json; from openlens.main import create_app; \

# json.dump(create_app().openapi(), open('frontend/openapi/openapi.json','w'), indent=2)"

#

# The generated client uses global `fetch`, so vitest fetch stubs work unchanged.
