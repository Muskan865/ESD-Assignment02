"""Payment endpoints and the fault-injection controls used in Part E."""

import random
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .state import CHAOS, LOCK, claim_forced_failure, reset_chaos, snapshot
from .structured_logging import log_event
from .telemetry import CHARGE_AMOUNT, CHARGES

router = APIRouter()


class ChargeRequest(BaseModel):
    order_id: int
    amount_cents: int = Field(gt=0)
    # Deliberately NOT a card number. The app never sees or logs one; this is a
    # bounded, non-identifying label so the metric stays low-cardinality.
    method: str = Field(default="card", pattern="^(card|wallet|cod)$")


@router.post("/payment/charge")
def charge(payload: ChargeRequest, request: Request):
    """Authorise one order payment.

    Three outcomes, all recorded on `payment_charges_total{outcome=...}`:
      approved -> 200, declined -> 402, error -> 503.
    """
    request_id = getattr(request.state, "request_id", "-")
    chaos = snapshot()

    # --- injected latency (Part E fault) ---
    delayed = False
    if chaos["latency_probability"] > 0 and random.random() < chaos["latency_probability"]:
        delayed = True
        time.sleep(chaos["latency_seconds"])

    # --- injected hard failure (Part E fault) ---
    forced = claim_forced_failure()
    if forced or (chaos["failure_rate"] > 0 and random.random() < chaos["failure_rate"]):
        CHARGES.labels(outcome="error").inc()
        log_event(
            "charge failed at provider",
            level="error",
            event={"action": "payment.charge", "outcome": "error"},
            order={"id": payload.order_id},
            fault={"injected": True, "delayed": delayed},
            request_id=request_id,
        )
        raise HTTPException(status_code=503, detail="payment provider unavailable")

    # --- normal path: a small deterministic decline rate keeps the "declined"
    #     series non-empty so the Grafana panel is meaningful without chaos. ---
    approved = random.random() > 0.03
    outcome = "approved" if approved else "declined"
    CHARGES.labels(outcome=outcome).inc()

    if approved:
        CHARGE_AMOUNT.observe(payload.amount_cents / 100.0)
        log_event(
            "charge approved",
            event={"action": "payment.charge", "outcome": "approved"},
            order={"id": payload.order_id, "amount_dollars": round(payload.amount_cents / 100.0, 2)},
            payment={"method": payload.method},
            fault={"injected": delayed, "delayed": delayed},
            request_id=request_id,
        )
        return {"status": "approved", "order_id": payload.order_id, "delayed": delayed}

    log_event(
        "charge declined",
        level="warn",
        event={"action": "payment.charge", "outcome": "declined"},
        order={"id": payload.order_id},
        payment={"method": payload.method},
        request_id=request_id,
    )
    raise HTTPException(status_code=402, detail="card declined")


# --------------------------------------------------------------------- chaos
class LatencyFault(BaseModel):
    probability: float = Field(ge=0.0, le=1.0)
    seconds: float = Field(default=0.5, ge=0.0, le=10.0)


class FailureFault(BaseModel):
    rate: float = Field(ge=0.0, le=1.0)
    fail_next: int = Field(default=0, ge=0, le=1000)


@router.post("/chaos/latency")
def set_latency(fault: LatencyFault):
    """Delay a fraction of charges. `probability=0.2, seconds=0.5` is the
    'add 500 ms to every fifth request' scenario from the assignment."""
    with LOCK:
        CHAOS["latency_probability"] = fault.probability
        CHAOS["latency_seconds"] = fault.seconds
    log_event("chaos: latency fault set", level="warn", fault=fault.model_dump())
    return snapshot()


@router.post("/chaos/failure")
def set_failure(fault: FailureFault):
    """Answer a fraction of charges, and/or exactly the next `fail_next`, with 503."""
    with LOCK:
        CHAOS["failure_rate"] = fault.rate
        CHAOS["fail_next"] = fault.fail_next
    log_event("chaos: failure fault set", level="warn", fault=fault.model_dump())
    return snapshot()


@router.post("/chaos/reset")
def reset():
    """Clear every injected fault. Always safe to call."""
    state = reset_chaos()
    log_event("chaos: reset", level="warn")
    return state


@router.get("/chaos/status")
def status():
    return snapshot()
