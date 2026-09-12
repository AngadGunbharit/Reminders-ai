"""Call state machine.

The LLM does not freestyle the whole conversation. Each turn, the
orchestrator (app/agent.py) tells the model which state it's in and which
tools are valid to call from that state; the model can only move the state
forward by calling `advance_state`, and any turn can be pre-empted by an
interrupt (dispute, hardship, legal threat, manager request, anger,
failed identity check) that jumps straight to ESCALATED or END regardless
of the current step.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class State(str, Enum):
    INTRO = "INTRO"
    IDENTIFY_CUSTOMER = "IDENTIFY_CUSTOMER"
    DISCUSS_BALANCE = "DISCUSS_BALANCE"
    UNDERSTAND_SITUATION = "UNDERSTAND_SITUATION"
    DETERMINE_ABILITY_TO_PAY = "DETERMINE_ABILITY_TO_PAY"
    CHECK_OPTIONS = "CHECK_OPTIONS"
    NEGOTIATE = "NEGOTIATE"
    CONFIRM_ACTION = "CONFIRM_ACTION"
    EXECUTE_ACTION = "EXECUTE_ACTION"
    SUMMARIZE = "SUMMARIZE"
    END = "END"
    ESCALATED = "ESCALATED"


# Happy-path linear flow.
HAPPY_PATH: dict[State, State] = {
    State.INTRO: State.IDENTIFY_CUSTOMER,
    State.IDENTIFY_CUSTOMER: State.DISCUSS_BALANCE,
    State.DISCUSS_BALANCE: State.UNDERSTAND_SITUATION,
    State.UNDERSTAND_SITUATION: State.DETERMINE_ABILITY_TO_PAY,
    State.DETERMINE_ABILITY_TO_PAY: State.CHECK_OPTIONS,
    State.CHECK_OPTIONS: State.NEGOTIATE,
    State.NEGOTIATE: State.CONFIRM_ACTION,
    State.CONFIRM_ACTION: State.EXECUTE_ACTION,
    State.EXECUTE_ACTION: State.SUMMARIZE,
    State.SUMMARIZE: State.END,
}

# Which tools are valid to call while in a given state. The orchestrator
# only offers the model these tools' schemas for the turn (plus the
# always-available escalate_to_human / add_account_note / advance_state).
STATE_TOOLS: dict[State, list[str]] = {
    State.INTRO: [],
    State.IDENTIFY_CUSTOMER: ["get_customer"],
    State.DISCUSS_BALANCE: [],
    State.UNDERSTAND_SITUATION: [],
    State.DETERMINE_ABILITY_TO_PAY: ["get_payment_options"],
    State.CHECK_OPTIONS: ["get_payment_options"],
    State.NEGOTIATE: ["get_payment_options"],
    State.CONFIRM_ACTION: ["get_payment_options"],
    State.EXECUTE_ACTION: ["make_payment", "schedule_payment"],
    State.SUMMARIZE: ["add_account_note"],
    State.END: [],
    State.ESCALATED: ["escalate_to_human", "add_account_note"],
}

ALWAYS_AVAILABLE_TOOLS = ["escalate_to_human", "add_account_note", "advance_state"]

# Escape paths: any of these interrupts pre-empt the happy path from ANY state.
INTERRUPT_KEYWORDS: dict[str, list[str]] = {
    "dispute": [
        "never authorized", "not my invoice", "don't recognize this charge",
        "dispute", "never ordered", "not mine", "wrong customer", "never received",
    ],
    "hardship": [
        "lost my job", "laid off", "can't pay anything", "no income",
        "hardship", "medical bills", "bankrupt", "can't afford any",
    ],
    "legal_threat": [
        "lawyer", "attorney", "sue", "lawsuit", "legal action", "court",
    ],
    "manager_request": [
        "speak to a manager", "speak to your supervisor", "talk to a human",
        "let me speak to someone else",
    ],
    "angry_customer": [
        "this is ridiculous", "stop calling", "harassing", "harassment",
        "f*** you", "leave me alone", "sick of these calls",
    ],
}

# Interrupt -> policy action mapping mirrors app/policy.json's
# escalation_triggers, duplicated here only as a fallback default.
INTERRUPT_DEFAULT_ACTION = {
    "dispute": "escalate",
    "hardship": "escalate",
    "legal_threat": "escalate",
    "manager_request": "escalate",
    "angry_customer": "escalate",
}


def detect_interrupt(customer_message: str) -> Optional[str]:
    """Deterministic keyword backstop used by offline/simulation mode and as
    a safety net alongside the LLM's own judgment (the model is instructed
    to call escalate_to_human whenever it recognizes these situations, even
    if no keyword matches — sarcasm, indirect phrasing, etc.)."""
    text = customer_message.lower()
    for trigger, keywords in INTERRUPT_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return trigger
    return None


@dataclass
class CallStateMachine:
    current: State = State.INTRO
    history: list[State] = field(default_factory=lambda: [State.INTRO])

    def available_tools(self) -> list[str]:
        seen: set[str] = set()
        result = []
        for t in STATE_TOOLS.get(self.current, []) + ALWAYS_AVAILABLE_TOOLS:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result

    def goto(self, state: State) -> State:
        """Jump directly to a state. Used by the offline heuristic backend,
        which (unlike the LLM backend's one-step-at-a-time advance_state
        tool) sometimes needs to re-enter an earlier step of the happy path
        (e.g. NEGOTIATE -> CHECK_OPTIONS again after a counter-offer)."""
        self.current = state
        self.history.append(state)
        return self.current

    def advance(self) -> State:
        """Move one step along the happy path. No-op at terminal states."""
        nxt = HAPPY_PATH.get(self.current)
        if nxt is not None:
            self.current = nxt
            self.history.append(nxt)
        return self.current

    def escalate(self) -> State:
        self.current = State.ESCALATED
        self.history.append(State.ESCALATED)
        return self.current

    def end(self) -> State:
        self.current = State.END
        self.history.append(State.END)
        return self.current

    def is_terminal(self) -> bool:
        return self.current in (State.END, State.ESCALATED)
