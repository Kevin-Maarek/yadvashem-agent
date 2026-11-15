import hashlib
import json


def fingerprint(*values) -> str:
    serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

