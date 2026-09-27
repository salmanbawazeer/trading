"""Buy/sell tallies for the dashboard, from per-side aggregates."""

from __future__ import annotations

from collections.abc import Iterable


def aggregate(fills: Iterable[tuple[str, float, float, float]]) -> dict[str, tuple[int, float, float, float]]:
    """(side, price, size, fee) rows -> {side: (count, qty, notional, fees)}."""
    out: dict[str, list[float]] = {"BUY": [0, 0.0, 0.0, 0.0], "SELL": [0, 0.0, 0.0, 0.0]}
    for side, price, size, fee in fills:
        a = out.setdefault(side, [0, 0.0, 0.0, 0.0])
        a[0] += 1
        a[1] += size
        a[2] += price * size
        a[3] += fee
    return {k: (int(v[0]), v[1], v[2], v[3]) for k, v in out.items()}


def summarize(agg: dict[str, tuple[int, float, float, float]]) -> dict:
    """Counts, volumes, average prices, completed pairs and captured spread."""
    b_n, b_q, b_notional, b_fee = agg.get("BUY", (0, 0.0, 0.0, 0.0))
    s_n, s_q, s_notional, s_fee = agg.get("SELL", (0, 0.0, 0.0, 0.0))
    avg_buy = b_notional / b_q if b_q else None
    avg_sell = s_notional / s_q if s_q else None
    matched = min(b_q, s_q)
    captured = matched * (avg_sell - avg_buy) if (avg_buy is not None and avg_sell is not None) else 0.0
    fees = b_fee + s_fee
    return {
        "buys": b_n,
        "sells": s_n,
        "pairs": min(b_n, s_n),
        "buy_usdt": b_q,
        "sell_usdt": s_q,
        "avg_buy": avg_buy,
        "avg_sell": avg_sell,
        "matched_usdt": matched,
        "captured_gbp": captured,
        "fees_gbp": fees,
        "net_gbp": captured - fees,
    }
