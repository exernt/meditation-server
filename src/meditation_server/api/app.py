from __future__ import annotations

import html
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from meditation_server import settings
from meditation_server.api import auth, feedback, generation_jobs, questionnaires, response_exports, studies, study_questionnaires
from meditation_server.api.dependencies import CurrentUserDependency, DatabaseDependency, create_executor
from meditation_server.api.models import ScriptWithParkRequest, ScriptWithoutParkRequest, TtsRequest, WalkRouteRequest
from meditation_server.integrations.maps import generate_walk_route
from meditation_server.integrations.scripts import with_park, without_park
from meditation_server.integrations.speech import InvalidSpeechText, SpeechDisabled, synthesize
from meditation_server.repositories.asyncpg import AsyncpgExecutor


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.database = await create_executor()
    try:
        yield
    finally:
        database = application.state.database
        if isinstance(database, AsyncpgExecutor):
            await database.close()


app = FastAPI(lifespan=lifespan)
app.include_router(auth.router)
app.include_router(studies.router)
app.include_router(questionnaires.router)
app.include_router(study_questionnaires.router)
app.include_router(response_exports.router)
app.include_router(feedback.router)
app.include_router(generation_jobs.router)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.url.path.startswith(("/auth", "/studies", "/questionnaires", "/response-exports", "/feedback", "/route", "/tts", "/v1")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def root():
    return {"Heartbeat": "Hello World"}


@app.get("/health")
async def health():
    return {"status": "ready", "component": "api"}


@app.get("/health/database")
async def database_health(database: DatabaseDependency):
    try:
        await database.execute("SELECT 1 AS ready")
    except Exception as exc:
        logger.warning("Database readiness check failed", exc_info=True)
        raise HTTPException(status_code=503, detail="Database is unavailable") from exc
    return {"database": "ready"}


@app.get("/.well-known/assetlinks.json", response_model=None)
async def asset_links():
    fingerprints = settings.android_fingerprints()
    if not fingerprints:
        raise HTTPException(status_code=404, detail="App Link verification is not configured")
    return JSONResponse([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": settings.android_package_name(),
            "sha256_cert_fingerprints": fingerprints,
        },
    }])


@app.get("/auth/password-reset/open", response_model=None)
async def password_reset_page():
    return HTMLResponse(
        """<!doctype html><html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Reset Password - Walking Meditation</title></head><body style="font-family:sans-serif;text-align:center;padding:50px 20px"><h2>Open Walking Meditation</h2><p>Continue in the app to choose a new password.</p><p><a id="open-app" href="walkingmeditation://password-reset">Open the app</a></p><script>const target="walkingmeditation://password-reset"+window.location.hash;document.getElementById("open-app").href=target;window.location.replace(target);</script></body></html>""",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/study/join", response_model=None)
async def join_page(id: str = Query(min_length=1, max_length=128)):
    escaped = html.escape(id, quote=True)
    deep_link = f"walkingmeditation://study/join?id={escaped}"
    return HTMLResponse(f"""<!doctype html><html><head><title>Join Study - Walking Meditation</title><meta http-equiv="refresh" content="0;url={deep_link}"></head><body style="font-family:sans-serif;text-align:center;padding-top:50px"><h2>Opening Walking Meditation App...</h2><p>If the app does not open automatically, <a href="{deep_link}">click here to join</a>.</p></body></html>""")


@app.post("/route/generate-path")
async def route_generate(payload: WalkRouteRequest, _user: CurrentUserDependency):
    try:
        return await generate_walk_route(payload)
    except Exception as exc:
        logger.exception("Route generation failed")
        raise HTTPException(status_code=502, detail="Route generation failed") from exc


@app.post("/route/generate-script/park")
async def generate_park_script(payload: ScriptWithParkRequest, _user: CurrentUserDependency):
    try:
        return await with_park(payload)
    except Exception as exc:
        logger.exception("Park script generation failed")
        raise HTTPException(status_code=502, detail="Script generation failed") from exc


@app.post("/route/generate-script/without-park")
async def generate_baseline_script(payload: ScriptWithoutParkRequest, _user: CurrentUserDependency):
    try:
        return await without_park(payload)
    except Exception as exc:
        logger.exception("Baseline script generation failed")
        raise HTTPException(status_code=502, detail="Script generation failed") from exc


@app.post("/tts")
async def text_to_speech(payload: TtsRequest, _user: CurrentUserDependency):
    try:
        audio = await synthesize(payload.text)
    except InvalidSpeechText as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SpeechDisabled as exc:
        raise HTTPException(status_code=503, detail="Text-to-speech is disabled") from exc
    except Exception as exc:
        logger.exception("TTS provider failed")
        raise HTTPException(status_code=502, detail="Text-to-speech is unavailable") from exc
    return Response(
        audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="narration.mp3"'},
    )
