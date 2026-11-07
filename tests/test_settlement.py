from keiba_acquisition.settlement import audit_settlement


def sample(size=8, *, cancelled=None, refunds=None):
    cancelled = cancelled or []
    entries = [
        {
            "horse_number": n,
            "finish": None if n in cancelled else n,
            "finish_text": "取消" if n in cancelled else str(n),
        }
        for n in range(1, size + 1)
    ]
    places = 3 if size - len(cancelled) >= 8 else 2
    payments = [("単勝", 1, 500)] + [("複勝", n, 100 + n * 10) for n in range(1, places + 1)]
    return html(payments, refunds), {
        "entries": entries,
        "payouts": [
            {"bet_type": kind, "horse_number": n, "yen_per_100": amount}
            for kind, n, amount in payments
        ],
    }


def html(payments, refunds=None):
    body = '<div class="refund_area"><div class="refund_unit"><ul>'
    for name, css in [("単勝", "win"), ("複勝", "place")]:
        body += f'<li class="{css}"><dl><dt>{name}</dt><dd>'
        for kind, n, amount in payments:
            if kind == name:
                body += f'<div class="line"><div class="num">{n}</div><div class="yen">{amount:,}円</div></div>'
        body += "</dd></dl></li>"
    body += "</ul></div>"
    if refunds:
        body += (
            '<div class="refund_unit restoration"><dl><dt>返還</dt><dd>返還馬番'
            + "".join(f"{n}番" for n in refunds)
            + "返還同枠4枠</dd></dl></div>"
        )
    return (body + '<div class="caution">的中がない場合は特払い</div></div>').encode()


def test_payout_evidence_confirms_zero_losses_but_not_missing_data():
    body, result = sample()
    audit = audit_settlement(body, result)
    assert audit["issues"] == []
    assert audit["entries"][-1]["bets"]["単勝"] == {
        "eligible": True,
        "reason": "verified",
        "hit": False,
        "payout_yen": 0,
    }
    unknown = audit_settlement(b"<p>maintenance</p>", result)
    assert unknown["entries"][-1]["bets"]["単勝"]["payout_yen"] is None


def test_refunds_and_cancellations_are_distinct_and_not_purchases():
    body, result = sample(9, cancelled=[9], refunds=[9])
    audit = audit_settlement(body, result)
    assert audit["issues"] == []
    assert audit["entries"][-1]["bets"]["単勝"]["reason"] == "refunded"
    no_refund = audit_settlement(*sample(9, cancelled=[9]))
    assert no_refund["entries"][-1]["bets"]["単勝"]["reason"] == "nonstarter"


def test_two_place_payments_for_seven_starters_and_ambiguous_sale_boundary():
    audit = audit_settlement(*sample(7))
    assert audit["issues"] == []
    assert audit["entries"][2]["bets"]["複勝"]["hit"] is False
    body, result = sample(8, cancelled=[8])
    assert "複勝:unknown_place_terms" in audit_settlement(body, result)["issues"]
    assert audit_settlement(body, result, place_slots=2)["issues"] == []


def test_incomplete_place_block_cannot_turn_the_third_winner_into_a_loss():
    body, result = sample()
    result["payouts"] = [
        p for p in result["payouts"] if not (p["bet_type"] == "複勝" and p["horse_number"] == 3)
    ]
    payments = [(p["bet_type"], p["horse_number"], p["yen_per_100"]) for p in result["payouts"]]
    audit = audit_settlement(html(payments), result)
    assert "複勝:place_rank_mismatch" in audit["issues"]
    assert audit["entries"][2]["bets"]["複勝"]["payout_yen"] is None
    assert audit["bets"]["単勝"]["status"] == "verified"


def test_dead_heat_uses_both_official_winning_payments():
    _, result = sample()
    result["entries"][1].update(finish=1, finish_text="1")
    payments = [
        ("単勝", 1, 300),
        ("単勝", 2, 400),
        ("複勝", 1, 150),
        ("複勝", 2, 150),
        ("複勝", 3, 180),
    ]
    result["payouts"] = [
        {"bet_type": k, "horse_number": n, "yen_per_100": a} for k, n, a in payments
    ]
    audit = audit_settlement(html(payments), result)
    assert audit["issues"] == []
    assert [e["bets"]["単勝"]["payout_yen"] for e in audit["entries"][:2]] == [300, 400]
