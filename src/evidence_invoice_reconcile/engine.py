from decimal import Decimal, ROUND_HALF_UP

def calculate(rows, payload):
    seen = {}; exceptions = []; net = Decimal("0.00")
    for row in rows:
        number = row["number"]; seen[number] = seen.get(number, 0) + 1
        expected = (Decimal(row["net"]) * Decimal(row["tax_rate"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if expected != Decimal(row["tax"]): exceptions.append({"number": number, "reason": "tax_mismatch"})
        net += Decimal(row["net"]) + Decimal(row["tax"])
    for number, count in seen.items():
        if count > 1 and not number.endswith("-R"): exceptions.append({"number": number, "reason": "duplicate_number"})
    return {"gross_total": str(net.quantize(Decimal("0.01"))), "exceptions": exceptions, "paid_numbers": payload.get("payments", [])}
