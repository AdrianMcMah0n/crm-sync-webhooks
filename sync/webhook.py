"""Webhook receiver for web-form submissions.

The form tool signs each request body with a shared secret: header X-Signature: sha256=<hex HMAC>.
Unsigned or wrongly signed requests get 401 and are never processed. Valid ones get 202 at once and are
processed in the background, so the form tool is never kept waiting by a slow CRM.
"""
import hashlib
import hmac
import json

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request


def sign(secret: str, body: bytes) -> str:
    return 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def create_app(pipeline, secret: str) -> FastAPI:
    app = FastAPI(title='Form webhook')
    app.state.rejected_signatures = 0

    @app.post('/webhooks/form', status_code=202)
    async def receive(request: Request, background: BackgroundTasks, x_signature: str | None = Header(None)):
        body = await request.body()
        if not x_signature or not hmac.compare_digest(x_signature, sign(secret, body)):
            app.state.rejected_signatures += 1
            raise HTTPException(401, 'bad signature')
        submission = json.loads(body)
        if 'submission_id' not in submission:
            raise HTTPException(422, 'submission_id is required')
        background.add_task(pipeline.process, submission)
        return {'accepted': submission['submission_id']}

    return app
