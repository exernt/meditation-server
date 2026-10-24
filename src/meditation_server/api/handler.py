from __future__ import annotations

from mangum import Mangum

from meditation_server.api.app import app


# API Gateway REST API payload v1 adapter. Lifespan initializes the shared
# database executor once per Lambda execution environment.
handler = Mangum(app, lifespan="auto", api_gateway_base_path=None)

