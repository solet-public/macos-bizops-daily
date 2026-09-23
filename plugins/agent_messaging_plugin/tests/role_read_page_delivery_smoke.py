#!/usr/bin/env python3
"""Focused wake-role receipt record recognition tests."""

from __future__ import annotations

import json

from agent_messaging_plugin.local_cli.wake_role_receipts import _role_pair


def main() -> int:
    live = json.dumps({"watch": "event", "event": {"meta": {
        "recipient_kind": "role", "recipient_key": "Main", "role_row_id": "arr-1",
    }}})
    catch_up = json.dumps({"watch": "inbox", "section": "role_entries", "entry": {
        "message": {"metadata": {
            "recipient_kind": "role", "recipient_key": "Main", "role_row_id": "arr-2",
        }},
    }})
    assert _role_pair(live) == ("Main", "arr-1")
    assert _role_pair(catch_up) == ("Main", "arr-2")
    assert _role_pair("not json") is None
    assert _role_pair(json.dumps({"watch": "event", "event": {"meta": {}}})) is None
    print("role_read_page_delivery_smoke: exact live/catch-up recognition passes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
