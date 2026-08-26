"""Edit MATCHER/VALUE_1/VALUE_2/CONFIG below and rerun.

    docker cp scripts/manual_matcher_check.py recon-app-1:/app/scripts/manual_matcher_check.py
    docker exec recon-app-1 sh -c "cd /app && PYTHONPATH=/app python3 scripts/manual_matcher_check.py"
"""
from app.reconciliation.rules.matchers import MATCHER_REGISTRY

MATCHER = "numeric_suffix"  # exact | substring | numeric_suffix | token_overlap
VALUE_1 = "NEFT TRANSFER REF bestT04 INVC 1046"  # the "bank" value
VALUE_2 = "KEST04"  # the "candidate" value

VALUE3 = "UPI/RAHUL SHARMA/ORDER 98231/ICICI"
VALUE4 = "UPI PAYMENT FROM RAHUL SHARMA FOR ORDER 98231" 
CONFIG = {}  # e.g. {"suffix_length": 4} for numeric_suffix

result = MATCHER_REGISTRY[MATCHER](VALUE_1, VALUE_2, CONFIG)
print(f"{MATCHER}({VALUE_1!r}, {VALUE_2!r}, {CONFIG}) -> {result}")
