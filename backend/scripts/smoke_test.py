#!/usr/bin/env python3
"""Drive a running ARIA backend end to end and print what happened.

Unlike ``pytest backend/tests`` (which runs in-process and offline), this talks
to a live server over HTTP — use it to verify a real deployment, including the
audio path and whichever model is installed.

    python backend/main.py &                       # in another terminal
    python backend/scripts/smoke_test.py                       # typed reports
    python backend/scripts/smoke_test.py --audio noisy_input   # audio too

Exits non-zero if any step fails, so it works as a CI or pre-demo check.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:8000"

REPORTS = [
    "He collapsed in the corridor, he is not breathing and I cannot find a pulse",
    "Elderly woman fell down the steps, her leg looks wrong and she cannot walk",
    "My neighbour uncle is not moving and his legs look strange",
    "Family of four in bay twelve, no water and nothing to eat since yesterday",
]


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{method} {path} → HTTP {exc.code}: {exc.read().decode()[:400]}")
        except urllib.error.URLError as exc:
            raise SystemExit(f"Cannot reach {self.base} ({exc.reason}). Is the backend running?")
        return json.loads(body) if body else {}


def show_request(request: dict, elapsed: float) -> None:
    print(f"\n  {request['request_id']}  {request['severity']:8}  ({elapsed:.1f}s)")
    print(f"    “{request['transcript'][:90]}”")
    for situation in request["situations"]:
        materials = ", ".join(
            f"{m['item']}×{m['quantity']}{'' if m['available'] else ' (unavailable)'}"
            for m in situation["materials"]
        )
        print(
            f"    · {situation['severity']:8} {situation['confidence']:.2f} "
            f"[{situation['origin']}] {situation['label']}"
        )
        if materials:
            print(f"        needs: {materials}")
    for note in request.get("notes", []):
        print(f"    ! {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--audio", type=Path, help="Directory of .wav files to submit too")
    parser.add_argument("--keep", action="store_true", help="Leave the created requests queued")
    args = parser.parse_args(argv)

    client = Client(args.base)

    health = client.call("GET", "/health/detail")
    print(f"ARIA {health['version']} — {health['status']}")
    for component in health["components"]:
        print(f"  {'ok ' if component['ok'] else 'NO '} {component['name']:16} {component['detail']}")

    created: list[str] = []

    print("\n── Typed intake ──")
    for report in REPORTS:
        started = time.time()
        payload = client.call("POST", "/pipeline/text", {"text": report})
        show_request(payload["request"], time.time() - started)
        created.append(payload["request"]["request_id"])

    if args.audio:
        files = sorted(args.audio.glob("*.wav"))
        print(f"\n── Audio intake ({len(files)} file(s)) ──")
        for path in files:
            started = time.time()
            encoded = base64.b64encode(path.read_bytes()).decode()
            payload = client.call(
                "POST", "/pipeline", {"audio_b64": encoded, "filename": path.name}
            )
            print(f"  {path.name}")
            show_request(payload["request"], time.time() - started)
            created.append(payload["request"]["request_id"])

    print("\n── Approve and dispatch ──")
    for request_id in created:
        result = client.call("POST", f"/requests/{request_id}/approve", {"selected_indices": [0]})
        request = result["request"]
        print(
            f"  {request_id} → {request['status']}"
            f"{' via ' + request['assigned_volunteer'] if request['assigned_volunteer'] else ''}"
        )

    board = client.call("GET", "/board")
    print("\n── Board ──")
    for request in board["queue"]:
        print(
            f"  {request['request_id']:12} {request['severity']:8} {request['status']:16}"
            f" {request['assigned_volunteer'] or '—':6} {request['situations'][0]['label']}"
        )
    metrics = board["metrics"]
    print(
        f"\n  open={metrics['requests']['open']} "
        f"volunteers busy={metrics['volunteers']['busy']}/{metrics['volunteers']['total']} "
        f"stock={metrics['inventory']['fill_pct']}% "
        f"sla breaches={metrics['sla']['breaching_now']}"
    )

    if not args.keep:
        print("\n── Cleanup ──")
        for request in board["queue"]:
            if request["status"] == "ASSIGNED":
                volunteer = request["assigned_volunteer"]
                client.call(
                    "POST",
                    f"/volunteers/{volunteer}/return",
                    {"returned_items": request["items_taken"], "note": "smoke test"},
                )
                print(f"  {volunteer} returned from {request['request_id']}")
            elif request["status"] in {"QUEUED", "AWAITING_REVIEW"}:
                client.call(
                    "POST", f"/requests/{request['request_id']}/cancel", {"reason": "smoke test"}
                )
                print(f"  cancelled {request['request_id']}")

    print("\nSmoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
