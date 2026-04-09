"""
Executor: runs the approved action_plan against the PMS.

Key design choices:
  1. Re-checks availability immediately before create_reservation (Edge Case 4 —
     inventory may have changed while the system was waiting for human approval).
  2. Resolves the NEW_GUEST placeholder so the LLM can express "use the guest
     created in the previous step" without knowing the generated ID ahead of time.
  3. Returns a structured ExecutionResult per action so callers can surface
     errors without crashing the whole workflow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .pms import PMS, PMSError


@dataclass
class ActionResult:
    action: str
    success: bool
    result: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class ExecutionResult:
    results: list[ActionResult] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        return all(r.success for r in self.results)

    @property
    def summary(self) -> str:
        lines = []
        for r in self.results:
            icon = "✅" if r.success else "❌"
            if r.success:
                lines.append(f"  {icon} {r.action}: OK")
                # Show key IDs if present
                res = r.result
                if "id" in res:
                    lines.append(f"       ID: {res['id']}")
                if "total_amount" in res:
                    lines.append(f"       Total: {res['total_amount']:,.0f} NOK")
            else:
                lines.append(f"  {icon} {r.action}: FAILED — {r.error}")
        return "\n".join(lines)


def execute_plan(pms: PMS, action_plan: list[dict]) -> ExecutionResult:
    """
    Execute each action in the plan in order.

    NEW_GUEST resolution:
      If an action has guest_id == "NEW_GUEST", we substitute the ID produced
      by the most recent create_guest action in the same run.
    """
    exec_result = ExecutionResult()
    last_created_guest_id: str | None = None

    for step in action_plan:
        action = step["action"]
        params: dict[str, Any] = dict(step.get("params", {}))

        # ── Resolve NEW_GUEST placeholder ─────────────────────────────────
        if params.get("guest_id") == "NEW_GUEST":
            if last_created_guest_id is None:
                exec_result.results.append(
                    ActionResult(
                        action=action,
                        success=False,
                        error=(
                            "guest_id is 'NEW_GUEST' but no create_guest action "
                            "has run yet in this plan."
                        ),
                    )
                )
                continue
            params["guest_id"] = last_created_guest_id

        # ── Dispatch ──────────────────────────────────────────────────────
        try:
            if action == "create_guest":
                result = pms.create_guest(**params)
                last_created_guest_id = result["id"]
                exec_result.results.append(
                    ActionResult(action=action, success=True, result=result)
                )

            elif action == "create_reservation":
                # Edge Case 4: re-check availability right before writing,
                # because human-approval mode may have introduced a delay.
                avail = pms.get_availability(
                    params["room_type_id"], params["check_in"], params["check_out"]
                )
                if avail == 0:
                    exec_result.results.append(
                        ActionResult(
                            action=action,
                            success=False,
                            error=(
                                f"Room {params['room_type_id']} is no longer available "
                                f"for {params['check_in']} – {params['check_out']}. "
                                "Inventory may have changed since the plan was drafted. "
                                "Please search for alternatives."
                            ),
                        )
                    )
                    continue

                result = pms.create_reservation(**params)
                exec_result.results.append(
                    ActionResult(action=action, success=True, result=result)
                )

            elif action == "cancel_reservation":
                result = pms.cancel_reservation(**params)
                exec_result.results.append(
                    ActionResult(action=action, success=True, result=result)
                )

            elif action == "modify_reservation":
                res_id = params.pop("reservation_id")
                result = pms.modify_reservation(res_id, **params)
                exec_result.results.append(
                    ActionResult(action=action, success=True, result=result)
                )

            else:
                exec_result.results.append(
                    ActionResult(
                        action=action,
                        success=False,
                        error=f"Unknown action type: {action}",
                    )
                )

        except PMSError as exc:
            exec_result.results.append(
                ActionResult(action=action, success=False, error=str(exc))
            )
        except Exception as exc:  # noqa: BLE001
            exec_result.results.append(
                ActionResult(
                    action=action,
                    success=False,
                    error=f"Unexpected error: {exc}",
                )
            )

    # ── Persistence ───────────────────────────────────────────────────
    # If any action that mutates state succeeded, we save to disk.
    mutating_actions = {"create_guest", "create_reservation", "cancel_reservation", "modify_reservation"}
    if any(r.success and r.action in mutating_actions for r in exec_result.results):
        try:
            pms.save()
        except Exception as exc:  # noqa: BLE001
            exec_result.results.append(
                ActionResult(
                    action="save_pms_data",
                    success=False,
                    error=f"Failed to save PMS data: {exc}"
                )
            )

    return exec_result