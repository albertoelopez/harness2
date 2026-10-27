from decimal import Decimal


def total_amounts(rows):
    return sum((Decimal(row['amount']) for row in rows if Decimal(row['amount']) > 0), Decimal(0))
