"""BillingAdapter — the account/invoicing side of a support event, separate
from the game panel on purpose: in real hosting stacks, suspension and
payment live in a billing system (WHMCS-style), not the game panel
(Pterodactyl-style). A support event that's actually a billing mixup should
be worked from a billing panel, not a stray "Unsuspend" button bolted onto
server config.

Every scenario gets a BillingAdapter, defaulting to a boring "account fine"
state — most scenarios never touch it, and the panel just shows that.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Invoice:
    id: str
    amount: int
    status: str = "pending"    # pending | paid | failed
    due_date: str = ""
    note: str = ""


@dataclass(frozen=True)
class BillingSnapshot:
    account_status: str                   # active | overdue | suspended
    suspension_reason: str
    payment_method: dict                  # {last4, exp_month, exp_year, status}
    invoices: tuple[dict, ...]
    activity: tuple[str, ...] = ()


class BillingAdapter:
    def __init__(self):
        self.account_status = "active"
        self.suspension_reason = ""
        self.payment_method = {"last4": "", "exp_month": 0, "exp_year": 0,
                               "status": "valid"}
        self.invoices: dict[str, Invoice] = {}
        self.activity: list[str] = []

    # ── fault-script verbs (dispatched from the same list as game verbs;
    # non-billing actions are simply not matched here) ──
    def apply_fault(self, steps) -> None:
        for step in steps:
            action = step.action
            if action == "set_payment_method":
                if step.last4 is not None:
                    self.payment_method["last4"] = step.last4
                if step.exp_month is not None:
                    self.payment_method["exp_month"] = step.exp_month
                if step.exp_year is not None:
                    self.payment_method["exp_year"] = step.exp_year
                if step.card_status is not None:
                    self.payment_method["status"] = step.card_status
            elif action == "create_invoice":
                self.invoices[step.invoice_id] = Invoice(
                    id=step.invoice_id, amount=step.amount or 0,
                    status=step.status or "pending",
                    due_date=step.due_date or "", note=step.note or "")
            elif action == "fail_payment":
                inv = self.invoices.get(step.invoice_id)
                if inv:
                    inv.status = "failed"
                if self.account_status == "active":
                    self.account_status = "overdue"
                self._log(f"billing:payment.failed:{step.invoice_id}")
            elif action == "suspend_account":
                self.account_status = "suspended"
                self.suspension_reason = step.note or self.suspension_reason
                self._log("billing:account.suspended")

    # ── trainee/demo actions ──
    def update_payment_method(self, last4: str, exp_month: int, exp_year: int) -> None:
        self.payment_method.update(last4=last4, exp_month=exp_month,
                                   exp_year=exp_year, status="valid")
        self._log("billing:card.updated_by_agent")

    def retry_payment(self, invoice_id: str) -> bool:
        inv = self.invoices.get(invoice_id)
        if inv is None:
            return False
        if self.payment_method["status"] != "valid":
            self._log(f"billing:retry.failed:{invoice_id}")
            return False
        inv.status = "paid"
        self._log(f"billing:retry.success:{invoice_id}")
        if self.account_status in ("overdue", "suspended") and all(
                i.status == "paid" for i in self.invoices.values()):
            self.account_status = "active"
            self.suspension_reason = ""
            self._log("billing:account.reactivated")
        return True

    # ── read ──
    def snapshot(self) -> BillingSnapshot:
        return BillingSnapshot(
            account_status=self.account_status,
            suspension_reason=self.suspension_reason,
            payment_method=dict(self.payment_method),
            invoices=tuple(vars(i) for i in self.invoices.values()),
            activity=tuple(self.activity),
        )

    def _log(self, event: str) -> None:
        self.activity.append(event)
