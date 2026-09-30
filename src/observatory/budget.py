"""Transactional reservations for every paid call, including embeddings.

Unknown outcomes retain their reservation as charged exposure until reconciliation.
Expired leases release concurrency, never money. All accounting months are UTC.
"""

from decimal import Decimal
from uuid import uuid4

from psycopg.types.json import Jsonb


class LimitReached(Exception):
    pass


def price(model, input_tokens, output_tokens=0, cached_tokens=0, cache_write_tokens=0):
    if model == "text-embedding-3-small":
        return Decimal(input_tokens) * Decimal("0.02") / 1_000_000
    if model != "gpt-5.6-luna":
        raise ValueError("No verified price configuration for this model")
    if (
        min(input_tokens, output_tokens, cached_tokens, cache_write_tokens) < 0
        or cached_tokens + cache_write_tokens > input_tokens
    ):
        raise ValueError("Invalid token accounting")
    return (
        Decimal(input_tokens - cached_tokens - cache_write_tokens) * Decimal("0.20")
        + Decimal(cached_tokens) * Decimal("0.02")
        + Decimal(cache_write_tokens) * Decimal("0.25")
        + Decimal(output_tokens) * Decimal("1.20")
    ) / 1_000_000


class Budget:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    def reserve(self, amount, visitor, kind, model):
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("Reservation must be positive")
        rid = uuid4().hex
        with self.db.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(54902)")
            month = conn.execute(
                "SELECT to_char(now() AT TIME ZONE 'UTC','YYYY-MM') AS month"
            ).fetchone()["month"]
            # Crashed or timed-out requests remain fully reserved in spend totals.
            conn.execute(
                "UPDATE usage_ledger SET state='uncertain' WHERE state='pending' AND expires_at<now()"
            )
            spent = conn.execute(
                "SELECT COALESCE(sum(COALESCE(actual_usd,reserved_usd)),0) AS n FROM usage_ledger WHERE month=%s AND state<>'cancelled'",
                (month,),
            ).fetchone()["n"]
            if spent + amount > self.settings.monthly_budget_usd:
                raise LimitReached(
                    "Monthly API budget reached. Browsing and keyword search remain available."
                )
            if kind == "generation":
                count = conn.execute(
                    "SELECT count(*) FILTER(WHERE created_at>now()-interval '1 minute') AS minute,count(*) FILTER(WHERE created_at>=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS day FROM usage_ledger WHERE visitor=%s AND kind='generation'",
                    (visitor,),
                ).fetchone()
                if (
                    count["minute"] >= self.settings.requests_per_minute
                    or count["day"] >= self.settings.requests_per_day
                ):
                    raise LimitReached(
                        "Your question limit has been reached. Please use keyword search or try later."
                    )
                active = conn.execute(
                    "SELECT count(*) AS n FROM usage_ledger WHERE kind='generation' AND state='pending'"
                ).fetchone()["n"]
                if active >= self.settings.concurrency:
                    raise LimitReached(
                        "Two answers are already in progress. Please try again shortly."
                    )
            conn.execute(
                "INSERT INTO usage_ledger(reservation_id,month,visitor,kind,model,reserved_usd,state,expires_at) VALUES (%s,%s,%s,%s,%s,%s,'pending',now()+interval '180 seconds')",
                (rid, month, visitor, kind, model, amount),
            )
        return rid

    def settle(self, rid, actual, usage):
        with self.db.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(54902)")
            row = conn.execute(
                "SELECT state FROM usage_ledger WHERE reservation_id=%s FOR UPDATE",
                (rid,),
            ).fetchone()
            if not row:
                raise ValueError("Unknown reservation")
            if row["state"] not in ("pending", "uncertain"):
                raise ValueError("Reservation already reconciled")
            conn.execute(
                "UPDATE usage_ledger SET actual_usd=%s,state='settled',usage=%s WHERE reservation_id=%s",
                (actual, Jsonb(usage), rid),
            )

    def uncertain(self, rid, error_type):
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE usage_ledger SET state='uncertain',usage=%s WHERE reservation_id=%s AND state='pending'",
                (Jsonb({"error_type": error_type}), rid),
            )

    def cancel_unsent(self, rid):
        """Only for calls known not to have been dispatched; never after a timeout."""
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE usage_ledger SET state='cancelled',actual_usd=0 WHERE reservation_id=%s AND state='pending'",
                (rid,),
            )

    def summary(self):
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT month,count(*) AS calls,sum(COALESCE(actual_usd,reserved_usd)) AS exposure_usd,count(*) FILTER(WHERE state='uncertain') AS uncertain_calls FROM usage_ledger WHERE state<>'cancelled' GROUP BY month ORDER BY month"
            ).fetchall()

    def reservation_cost(self, rid):
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(actual_usd,reserved_usd) AS cost FROM usage_ledger WHERE reservation_id=%s",
                (rid,),
            ).fetchone()
            return float(row["cost"]) if row else 0.0
