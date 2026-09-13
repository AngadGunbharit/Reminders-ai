"""Ava — general-purpose AI reminder/notification agent.

Drives a short, natural phone conversation based on a caller-supplied
purpose string. The only tool the LLM can call is end_call(), which
logs the outcome and terminates the loop.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from . import db

END_CALL_TOOL = {
    "name": "end_call",
    "description": (
        "Call this when the conversation is naturally complete — the contact has "
        "confirmed, declined, asked to be called back, or you've delivered the "
        "message and they've responded. Always end the call properly with this tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": ["confirmed", "declined", "callback_requested", "wrong_number", "completed"],
                "description": (
                    "confirmed — they agreed/will attend; "
                    "declined — they said no/can't make it; "
                    "callback_requested — they asked to be called later; "
                    "wrong_number — not the right person; "
                    "completed — message delivered, no specific outcome"
                ),
            },
            "notes": {
                "type": "string",
                "description": "One sentence summarising what was agreed or said.",
            },
        },
        "required": ["outcome"],
    },
}

OUTCOME_EMOJI = {
    "confirmed": "✅",
    "declined": "❌",
    "callback_requested": "📞",
    "wrong_number": "❓",
    "completed": "💬",
}


@dataclass
class ReminderAgent:
    contact_name: str
    contact_phone: str
    purpose: str
    use_llm: bool = field(default_factory=lambda: bool(os.environ.get("ANTHROPIC_API_KEY")))

    call_id: str = field(init=False)
    turn_index: int = field(default=0, init=False)
    _messages: list = field(default_factory=list, init=False)
    _client: Any = field(default=None, init=False)
    _outcome: Optional[str] = field(default=None, init=False)
    _notes: str = field(default="", init=False)

    def __post_init__(self):
        with db.tx() as conn:
            self.call_id = db.insert(conn, "calls", {
                "contact_name": self.contact_name,
                "contact_phone": self.contact_phone,
                "purpose": self.purpose,
                "started_at": db.now(),
                "status": "in_progress",
                "created_at": db.now(),
            })
        if self.use_llm:
            import anthropic
            self._client = anthropic.Anthropic()

    # ── System prompt ──────────────────────────────────────────────────

    def _system_prompt(self) -> str:
        return f"""You are Ava, a friendly and natural AI assistant making a phone call.

YOUR PURPOSE FOR THIS CALL:
{self.purpose}

GUIDELINES:
- Be warm, conversational, and concise — this is a real phone call, not an email
- Deliver the message naturally, then listen and respond to what they say
- Keep each reply short (1-3 sentences max)
- When the conversation is complete (they've confirmed, declined, or you've delivered
  the message and handled their response), call end_call with the outcome
- Never be pushy, robotic, or repeat yourself unnecessarily
- If they seem confused about who is calling, clarify warmly

Contact's name: {self.contact_name}
"""

    # ── Opening line ───────────────────────────────────────────────────

    def opening_line(self) -> str:
        """Generate opening via LLM (or a safe fallback)."""
        if self.use_llm and self._client:
            try:
                resp = self._client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=120,
                    system=self._system_prompt(),
                    messages=[{
                        "role": "user",
                        "content": (
                            "The phone just connected. Generate ONLY your opening line — "
                            "warm, brief, and natural. Do not include any preamble or explanation."
                        ),
                    }],
                )
                line = resp.content[0].text.strip()
            except Exception:
                line = self._fallback_opening()
        else:
            line = self._fallback_opening()

        self._log("agent", line)
        return line

    def _fallback_opening(self) -> str:
        return f"Hi {self.contact_name}, this is Ava — I'm calling with a quick message for you. Do you have a moment?"

    # ── Conversation step ──────────────────────────────────────────────

    def step(self, contact_message: str) -> tuple[str, bool]:
        """Process one contact utterance. Returns (agent_reply, is_ended)."""
        self._log("contact", contact_message)
        self._messages.append({"role": "user", "content": contact_message})

        ended = False
        final_text = ""

        while True:
            response = self._client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                system=self._system_prompt(),
                tools=[END_CALL_TOOL],
                messages=self._messages,
            )
            self._messages.append({"role": "assistant", "content": response.content})

            text_parts = [b.text for b in response.content if b.type == "text"]
            tool_uses  = [b for b in response.content if b.type == "tool_use"]

            if text_parts:
                final_text = " ".join(text_parts).strip()

            tool_results = []
            for call in tool_uses:
                if call.name == "end_call":
                    self._outcome = call.input.get("outcome", "completed")
                    self._notes   = call.input.get("notes", "")
                    ended = True
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": "Call ended.",
                    })

            if tool_results:
                self._messages.append({"role": "user", "content": tool_results})

            if response.stop_reason != "tool_use":
                break

        if final_text:
            self._log("agent", final_text)

        return final_text or "...", ended

    # ── Finalise ───────────────────────────────────────────────────────

    def finalize(self):
        with db.tx() as conn:
            conn.execute(
                "UPDATE calls SET ended_at=?, status=?, outcome=?, notes=? WHERE id=?",
                (db.now(), "completed", self._outcome or "completed", self._notes, self.call_id),
            )

    # ── Logging ────────────────────────────────────────────────────────

    def _log(self, speaker: str, message: str):
        with db.tx() as conn:
            db.insert(conn, "conversation_turns", {
                "call_id": self.call_id,
                "turn_index": self.turn_index,
                "speaker": speaker,
                "message": message,
                "created_at": db.now(),
            })
        self.turn_index += 1
