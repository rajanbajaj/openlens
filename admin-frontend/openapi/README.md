# Admin OpenAPI snapshot (checked in).

# This file is the contract Orval generates the admin API client

# from (`npm run api:gen` -> `src/api/generated/`). Regenerate it after

# backend route or schema changes with a live backend:

# uv run uvicorn openlens.main:app &

# curl -s http://127.0.0.1:8000/openapi.json | python -m json.tool > admin-frontend/openapi/openapi.json

# Or offline (routes only, no engine needed):

# OPENLENS_CONFIG=configs/default.yaml uv run python -c \

# "import json; from openlens.main import create_app; \

# json.dump(create_app().openapi(), open('admin-frontend/openapi/openapi.json','w'), indent=2)"

# Then run `npm run api:gen` inside `admin-frontend/` and commit the

# output. The generated client uses global `fetch`, so the existing

# vitest fetch stubs keep working unchanged.

# The admin snapshot MUST stay in sync with `frontend/openapi/openapi.json`

# for shared routes (`/api/v1/frontier/*`); it additionally covers the

# admin-only `/api/v1/admin/config/*` documents once those routes land.

# Regenerate only when backend routes change — never hand-edit output.
