"""FastAPI server for Ava Reminds.

Endpoints:
  GET  /                          → web dashboard
  GET  /api/calls                 → recent call history
  GET  /api/call/{id}/log         → transcript for a call
  POST /api/call/start            → trigger a single Vapi outbound call
  POST /api/campaign/start        → bulk CSV campaign (multiple contacts)
  GET  /api/campaigns             → list campaigns
  GET  /api/campaign/{id}         → campaign detail + calls
  POST /chat/completions          → Vapi custom-LLM webhook (OpenAI-compatible)

Required .env:
  ANTHROPIC_API_KEY          your Anthropic key
  VAPI_API_KEY               from app.vapi.ai → Account → API Keys (private key)
  VAPI_PHONE_NUMBER_ID       from app.vapi.ai → Phone Numbers
  SERVER_URL                 public HTTPS URL (e.g. ngrok tunnel)
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

load_dotenv(Path(__file__).parent / ".env")

from app import db
from app.reminder_agent import ReminderAgent, OUTCOME_EMOJI

app = FastAPI(title="Ava Reminds")

# Active agent sessions keyed by Vapi call ID
_sessions: dict[str, ReminderAgent] = {}


# ── Startup: launch background scheduler ───────────────────────────────
@app.on_event("startup")
async def startup():
    asyncio.create_task(_scheduler_loop())


# ── Web UI ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    return (Path(__file__).parent / "static" / "index.html").read_text()


# ── Recent calls ───────────────────────────────────────────────────────
@app.get("/api/calls")
def recent_calls():
    conn = db.get_connection()
    rows = conn.execute("""
        SELECT id, contact_name, contact_phone, purpose,
               started_at, ended_at, status, outcome, notes, scheduled_at
        FROM calls
        ORDER BY created_at DESC
        LIMIT 50
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Call transcript ────────────────────────────────────────────────────
@app.get("/api/call/{call_id}/log")
def call_log(call_id: str):
    conn = db.get_connection()
    call = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    if not call:
        conn.close()
        raise HTTPException(404, "Call not found")
    turns = conn.execute(
        "SELECT speaker, message, created_at FROM conversation_turns "
        "WHERE call_id = ? ORDER BY turn_index",
        (call_id,),
    ).fetchall()
    conn.close()
    return {
        "call": dict(call),
        "turns": [dict(t) for t in turns],
    }


# ── Start single outbound call ─────────────────────────────────────────
class CallRequest(BaseModel):
    contact_name: str
    phone_number: str   # E.164, e.g. "+14155550100"
    purpose: str
    scheduled_at: Optional[str] = None  # ISO datetime string or None


@app.post("/api/call/start")
async def start_call(body: CallRequest):
    call_id = str(uuid.uuid4())

    # If scheduled for the future, persist and return without calling Vapi now
    if body.scheduled_at:
        with db.tx() as conn:
            db.insert(conn, "calls", {
                "id": call_id,
                "campaign_id": None,
                "contact_id": None,
                "contact_name": body.contact_name,
                "contact_phone": body.phone_number,
                "purpose": body.purpose,
                "scheduled_at": body.scheduled_at,
                "status": "scheduled",
                "created_at": db.now(),
            })
        return {
            "internal_call_id": call_id,
            "status": "scheduled",
            "scheduled_at": body.scheduled_at,
        }

    return await _fire_call(
        call_id=call_id,
        contact_name=body.contact_name,
        phone_number=body.phone_number,
        purpose=body.purpose,
        campaign_id=None,
    )


# ── Campaign: bulk contacts ────────────────────────────────────────────
class Contact(BaseModel):
    name: str
    phone: str


class CampaignRequest(BaseModel):
    purpose: str
    contacts: list[Contact]
    scheduled_at: Optional[str] = None  # ISO datetime or None = send now


@app.post("/api/campaign/start")
async def start_campaign(body: CampaignRequest):
    campaign_id = str(uuid.uuid4())
    now = db.now()

    with db.tx() as conn:
        db.insert(conn, "campaigns", {
            "id": campaign_id,
            "purpose": body.purpose,
            "status": "pending" if body.scheduled_at else "running",
            "scheduled_at": body.scheduled_at,
            "total": len(body.contacts),
            "completed": 0,
            "created_at": now,
        })

        call_ids = []
        for contact in body.contacts:
            cid = str(uuid.uuid4())
            call_ids.append((cid, contact.name, contact.phone))
            db.insert(conn, "calls", {
                "id": cid,
                "campaign_id": campaign_id,
                "contact_id": None,
                "contact_name": contact.name,
                "contact_phone": contact.phone,
                "purpose": body.purpose,
                "scheduled_at": body.scheduled_at,
                "status": "scheduled" if body.scheduled_at else "pending",
                "created_at": now,
            })

    if not body.scheduled_at:
        # Fire immediately in background
        asyncio.create_task(_fire_campaign_calls(campaign_id, call_ids, body.purpose))

    return {
        "campaign_id": campaign_id,
        "total": len(body.contacts),
        "status": "scheduled" if body.scheduled_at else "running",
        "scheduled_at": body.scheduled_at,
    }


# ── Campaign list ──────────────────────────────────────────────────────
@app.get("/api/campaigns")
def list_campaigns():
    conn = db.get_connection()
    rows = conn.execute("""
        SELECT id, purpose, status, scheduled_at, total, completed, created_at
        FROM campaigns
        ORDER BY created_at DESC
        LIMIT 30
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Campaign detail ────────────────────────────────────────────────────
@app.get("/api/campaign/{campaign_id}")
def campaign_detail(campaign_id: str):
    conn = db.get_connection()
    campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if not campaign:
        conn.close()
        raise HTTPException(404, "Campaign not found")
    calls = conn.execute(
        "SELECT id, contact_name, contact_phone, status, outcome, notes, started_at, ended_at "
        "FROM calls WHERE campaign_id = ? ORDER BY created_at",
        (campaign_id,),
    ).fetchall()
    conn.close()
    return {
        "campaign": dict(campaign),
        "calls": [dict(c) for c in calls],
    }


# ── Vapi custom-LLM webhook ────────────────────────────────────────────
@app.post("/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    call_info    = body.get("call", {})
    vapi_call_id = call_info.get("id", "")
    metadata     = call_info.get("metadata", {})
    messages     = body.get("messages", [])

    user_turns = [m for m in messages if m.get("role") == "user"]
    if not user_turns:
        return _openai_response("...")

    last_user = user_turns[-1]["content"]

    agent = _sessions.get(vapi_call_id)
    if agent is None:
        # Session missing (e.g. server restart) — create a new call row then attach
        contact_name  = metadata.get("contact_name", "there")
        contact_phone = metadata.get("contact_phone", "")
        purpose       = metadata.get("purpose", "")
        call_id = str(uuid.uuid4())
        with db.tx() as conn:
            db.insert(conn, "calls", {
                "id": call_id,
                "campaign_id": None,
                "contact_id": None,
                "contact_name": contact_name,
                "contact_phone": contact_phone,
                "purpose": purpose,
                "scheduled_at": None,
                "status": "pending",
                "created_at": db.now(),
            })
        agent = ReminderAgent(
            contact_name=contact_name,
            contact_phone=contact_phone,
            purpose=purpose,
            use_llm=True,
        )
        agent.init_call(call_id)
        agent.opening_line()
        _sessions[vapi_call_id] = agent

    reply, ended = agent.step(last_user)

    if ended:
        agent.finalize()
        _sessions.pop(vapi_call_id, None)

    return _openai_response(reply)


# ── Internal helpers ───────────────────────────────────────────────────

async def _fire_call(
    call_id: str,
    contact_name: str,
    phone_number: str,
    purpose: str,
    campaign_id: Optional[str],
) -> dict:
    vapi_key   = os.environ.get("VAPI_API_KEY", "")
    phone_id   = os.environ.get("VAPI_PHONE_NUMBER_ID", "")
    server_url = os.environ.get("SERVER_URL", "").rstrip("/")

    if not all([vapi_key, phone_id, server_url]):
        raise HTTPException(400, "Set VAPI_API_KEY, VAPI_PHONE_NUMBER_ID, SERVER_URL in .env")

    agent = ReminderAgent(
        contact_name=contact_name,
        contact_phone=phone_number,
        purpose=purpose,
        use_llm=True,
    )

    # Persist the call row first so init_call can update it
    with db.tx() as conn:
        db.insert(conn, "calls", {
            "id": call_id,
            "campaign_id": campaign_id,
            "contact_id": None,
            "contact_name": contact_name,
            "contact_phone": phone_number,
            "purpose": purpose,
            "scheduled_at": None,
            "status": "pending",
            "created_at": db.now(),
        })

    agent.init_call(call_id)
    first_message = agent.opening_line()

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.vapi.ai/call/phone",
            headers={"Authorization": f"Bearer {vapi_key}"},
            json={
                "phoneNumberId": phone_id,
                "customer": {"number": phone_number},
                "assistant": {
                    "name": "Ava",
                    "model": {
                        "provider": "custom-llm",
                        "url": f"{server_url}/chat/completions",
                        "model": "ava-reminds",
                    },
                    "voice": {
                        "provider": "11labs",
                        "voiceId": "21m00Tcm4TlvDq8ikWAM",  # Rachel
                    },
                    "firstMessage": first_message,
                    "metadata": {
                        "contact_name": contact_name,
                        "contact_phone": phone_number,
                        "purpose": purpose,
                    },
                },
            },
        )

    if resp.status_code not in (200, 201):
        with db.tx() as conn:
            conn.execute("UPDATE calls SET status='failed' WHERE id=?", (call_id,))
        raise HTTPException(502, f"Vapi error {resp.status_code}: {resp.text}")

    vapi_call_id = resp.json()["id"]
    _sessions[vapi_call_id] = agent

    return {
        "vapi_call_id":     vapi_call_id,
        "internal_call_id": call_id,
        "status":           "initiated",
        "opening_line":     first_message,
    }


async def _fire_campaign_calls(campaign_id: str, call_ids: list, purpose: str):
    """Fire all calls for a campaign with a small stagger."""
    for call_id, contact_name, contact_phone in call_ids:
        try:
            await _fire_call(
                call_id=call_id,
                contact_name=contact_name,
                phone_number=contact_phone,
                purpose=purpose,
                campaign_id=campaign_id,
            )
        except Exception as exc:
            print(f"[campaign] Failed to fire call {call_id}: {exc}")
        await asyncio.sleep(2)  # stagger calls by 2 seconds

    with db.tx() as conn:
        conn.execute("UPDATE campaigns SET status='running' WHERE id=?", (campaign_id,))


async def _scheduler_loop():
    """Every 30 s: fire any scheduled calls/campaigns whose time has arrived."""
    while True:
        await asyncio.sleep(30)
        try:
            _process_due_calls()
        except Exception as exc:
            print(f"[scheduler] error: {exc}")


def _process_due_calls():
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = db.get_connection()
    due = conn.execute(
        "SELECT id, contact_name, contact_phone, purpose, campaign_id "
        "FROM calls WHERE status='scheduled' AND scheduled_at <= ?",
        (now_iso,),
    ).fetchall()
    conn.close()

    for row in due:
        asyncio.create_task(_fire_due_call(dict(row)))


async def _fire_due_call(row: dict):
    try:
        await _fire_call(
            call_id=row["id"],
            contact_name=row["contact_name"],
            phone_number=row["contact_phone"],
            purpose=row["purpose"],
            campaign_id=row["campaign_id"],
        )
    except Exception as exc:
        print(f"[scheduler] Failed to fire due call {row['id']}: {exc}")


def _openai_response(text: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
