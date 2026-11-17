"""SGD amounts are integer cents everywhere; floats never touch money."""
import re
from decimal import Decimal

MAX_CENTS = 1_000_000_00  # S$1,000,000 per figure: a typo guard, not a business rule

_AMOUNT = re.compile(r"[+-]?(\d+(\.\d{0,2})?|\.\d{1,2})")


def parse_cents(text: str) -> int:
    """Accepts '50', '-30.5', '+$1,200.00', 'S$5'; raises ValueError otherwise."""
    s = re.sub(r"S\$|[\s,$]", "", text.replace("−", "-"), flags=re.I)
    if not _AMOUNT.fullmatch(s):
        raise ValueError("Couldn't read that amount. Try 50 or -30.50.")
    cents = int(Decimal(s) * 100)
    if abs(cents) > MAX_CENTS:
        raise ValueError("That amount is too large. Check for typos.")
    return cents


def format_cents(cents: int, signed: bool = False, commas: bool = True) -> str:
    sign = "-" if cents < 0 else "+" if signed and cents > 0 else ""
    return f"{sign}{abs(cents) // 100:{',' if commas else ''}}.{abs(cents) % 100:02d}"


def settle(balances: dict[int, int]) -> list[tuple[int, int, int]]:
    """Greedy payer->payee transfers that zero every balance: (debtor, creditor, cents)."""
    debtors = sorted(((-c, k) for k, c in balances.items() if c < 0), reverse=True)
    creditors = sorted(((c, k) for k, c in balances.items() if c > 0), reverse=True)
    transfers, i, j = [], 0, 0
    while i < len(debtors) and j < len(creditors):
        owed, debtor = debtors[i]
        due, creditor = creditors[j]
        amount = min(owed, due)
        transfers.append((debtor, creditor, amount))
        debtors[i], creditors[j] = (owed - amount, debtor), (due - amount, creditor)
        i += debtors[i][0] == 0
        j += creditors[j][0] == 0
    return transfers
