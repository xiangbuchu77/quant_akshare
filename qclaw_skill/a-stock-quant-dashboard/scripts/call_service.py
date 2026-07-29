from __future__ import annotations

import json
import sys
import urllib.request


URL = "http://127.0.0.1:18766/qclaw/message"


def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    payload = raw.encode("utf-8")
    request = urllib.request.Request(
        URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
