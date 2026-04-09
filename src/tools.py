"""
Tools layer: bridges the LLM ↔ PMS.

Each tool has:
  1. A Python implementation   (_impl_*  functions)
  2. An Anthropic tool schema (collected in READ_TOOL_SCHEMAS)

There is one special "terminal" tool — `submit_plan` — that the agent calls
when it is ready to propose actions.  It has no side effects; it just
structures the agent's output.

Write tools (create_reservation, cancel_reservation, etc.) are NOT exposed to
the planning-phase LLM call.  They are executed by the Executor after the
plan is approved (or immediately in autonomous mode).
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from .pms import PMS
from .skills import SKILL_HANDLERS, SKILL_SCHEMAS


# ── Read-only tool implementations ────────────────────────────────────────────

def _impl_get_current_date(pms: PMS, current_date: date, **_) -> dict:
    return {
        "current_date": current_date.isoformat(),
        "day_of_week": current_date.strftime("%A"),
    }


def _impl_get_hotel_info(pms: PMS, **_) -> dict:
    return pms.get_hotel_info()


def _impl_get_guest_by_email(pms: PMS, email: str, **_) -> dict:
    guest = pms.get_guest_by_email(email)
    if guest is None:
        return {"found": False, "email": email}
    return {"found": True, "guest": guest}


def _impl_get_guest_reservations(pms: PMS, guest_id: str, **_) -> dict:
    reservations = pms.get_reservations_by_guest(guest_id)
    enriched = []
    for res in reservations:
        enriched.append(pms.get_reservation_enriched(res["id"]))
    return {"guest_id": guest_id, "reservations": enriched, "count": len(enriched)}


def _impl_get_reservation_details(pms: PMS, reservation_id: str, **_) -> dict:
    res = pms.get_reservation_enriched(reservation_id)
    if res is None:
        return {"found": False, "reservation_id": reservation_id}
    return {"found": True, "reservation": res}


def _impl_search_available_rooms(
    pms: PMS,
    check_in: str,
    check_out: str,
    adults: int,
    children: int = 0,
    **_,
) -> dict:
    results = pms.search_available_rooms(check_in, check_out, adults, children)
    return {
        "check_in": check_in,
        "check_out": check_out,
        "adults": adults,
        "children": children,
        "nights": len(pms._nights(check_in, check_out)),
        "available_rooms": results,
        "count": len(results),
    }


# ── Tool schemas (Anthropic tool-use format) ─────────────────────────────

READ_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_current_date",
        "description": (
            "Return today's date. Use this to resolve relative date expressions "
            "('next week', 'this weekend') and to evaluate cancellation windows."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_hotel_info",
        "description": (
            "Return hotel details and all policy text (cancellation, breakfast, "
            "parking, pets, extra beds, children). Consult before answering "
            "any policy question."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_guest_by_email",
        "description": (
            "Look up a guest profile by email address. "
            "Always call this before creating a new reservation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "The guest's email address (from the email header or body).",
                }
            },
            "required": ["email"],
        },
    },
    {
        "name": "get_guest_reservations",
        "description": "Retrieve all reservations for a guest by their guest ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "guest_id": {
                    "type": "string",
                    "description": "The guest's PMS ID (e.g. G001).",
                }
            },
            "required": ["guest_id"],
        },
    },
    {
        "name": "get_reservation_details",
        "description": (
            "Fetch full details for a single reservation by ID, including room type, "
            "rate plan, cancellation policy, and guest name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {
                    "type": "string",
                    "description": "The reservation ID (e.g. RES001).",
                }
            },
            "required": ["reservation_id"],
        },
    },
    {
        "name": "search_available_rooms",
        "description": (
            "Search for rooms available for a given date range and occupancy. "
            "Returns all matching room types with all rate plan options and exact pricing. "
            "Only returns rooms that satisfy occupancy constraints."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "check_in": {
                    "type": "string",
                    "description": "Check-in date in YYYY-MM-DD format.",
                },
                "check_out": {
                    "type": "string",
                    "description": (
                        "Check-out date in YYYY-MM-DD format. "
                        "The guest stays the nights between check_in and check_out "
                        "(e.g. Apr 20 – Apr 23 = 3 nights)."
                    ),
                },
                "adults": {
                    "type": "integer",
                    "description": "Number of adults.",
                    "minimum": 1,
                },
                "children": {
                    "type": "integer",
                    "description": "Number of children (default 0).",
                    "minimum": 0,
                },
            },
            "required": ["check_in", "check_out", "adults"],
        },
    },
]

# ── submit_plan — terminal tool ───────────────────────────────────────────────

SUBMIT_PLAN_SCHEMA: dict = {
    "name": "submit_plan",
    "description": (
        "Call this when you have gathered all the information you need. "
        "Submit the complete action plan, draft reply, and human-review flag. "
        "This ends the planning loop."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action_plan": {
                "type": "array",
                "description": (
                    "Ordered list of write operations to perform. "
                    "Empty if this is a read-only query or information is missing."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "create_guest",
                                "create_reservation",
                                "cancel_reservation",
                                "modify_reservation",
                            ],
                        },
                        "params": {
                            "type": "object",
                            "description": "Parameters for this action.",
                        },
                        "description": {
                            "type": "string",
                            "description": "Human-readable summary of this step.",
                        },
                    },
                    "required": ["action", "params", "description"],
                },
            },
            "draft_reply": {
                "type": "string",
                "description": "The full draft email reply to send to the guest.",
            },
            "requires_human_review": {
                "type": "boolean",
                "description": (
                    "True if this request must be reviewed by a human before "
                    "any action is taken (non-refundable refunds, complaints, etc.)."
                ),
            },
            "review_reason": {
                "type": "string",
                "description": (
                    "Required when requires_human_review is true. "
                    "Concise explanation of why human review is needed."
                ),
            },
        },
        "required": ["action_plan", "draft_reply", "requires_human_review"],
    },
}

# All tools passed to the LLM during the planning loop.
# Skills are listed FIRST so the LLM encounters and prefers them over
# calling individual read tools one at a time.
ALL_PLANNING_TOOLS = SKILL_SCHEMAS + READ_TOOL_SCHEMAS + [SUBMIT_PLAN_SCHEMA]


# ── Dispatcher ────────────────────────────────────────────────────────────────

_READ_HANDLERS: dict[str, Any] = {
    # ── Skills (multi-step workflows) ──────────────────────────────────────
    **SKILL_HANDLERS,
    # ── Atomic read tools (single PMS operations) ──────────────────────────
    "get_current_date": _impl_get_current_date,
    "get_hotel_info": _impl_get_hotel_info,
    "get_guest_by_email": _impl_get_guest_by_email,
    "get_guest_reservations": _impl_get_guest_reservations,
    "get_reservation_details": _impl_get_reservation_details,
    "search_available_rooms": _impl_search_available_rooms,
}


def dispatch_read_tool(
    tool_name: str,
    args: dict,
    pms: PMS,
    current_date: date,
) -> str:
    """
    Execute a read-only tool and return a JSON string result (for the
    Anthropic message history).
    """
    handler = _READ_HANDLERS.get(tool_name)
    if handler is None:
        return json.dumps({"error": f"Unknown read tool: {tool_name}"})
    try:
        result = handler(pms=pms, current_date=current_date, **args)
        return json.dumps(result, default=str)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})