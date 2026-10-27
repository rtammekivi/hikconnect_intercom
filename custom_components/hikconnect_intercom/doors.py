"""Bind entity slots to physical door stations by IP, not by station channel.

A multi-door indoor station re-slots its door stations when one reconnects, so
the channel the cloud reports for a door is not stable. Each entity keeps the
slot (its unique-ID channel) it was created for; the slot is pinned to the IP
of the door station that held it, and resolved to that door's current channel.
"""

from __future__ import annotations


def pin_doors(pins: dict[int, str], doors: dict[int, dict]) -> dict[int, str]:
    """Return ``pins`` (slot -> door IP) extended with any newly seen door."""
    out = dict(pins)
    counts: dict[str, int] = {}
    for door in doors.values():
        if door.get("ip"):
            counts[door["ip"]] = counts.get(door["ip"], 0) + 1
    for channel, door in sorted(doors.items()):
        ip = door.get("ip")
        if not ip or counts[ip] > 1 or ip in out.values():
            continue
        slot = channel if channel not in out else max([*out, *doors]) + 1
        out[slot] = ip
    return out


def resolve_slots(pins: dict[int, str], doors: dict[int, dict]) -> dict[int, int]:
    """Map each pinned slot whose door is present to that door's current channel."""
    by_ip: dict[str, list[int]] = {}
    for channel, door in doors.items():
        if door.get("ip"):
            by_ip.setdefault(door["ip"], []).append(channel)
    return {
        slot: by_ip[ip][0]
        for slot, ip in pins.items()
        if len(by_ip.get(ip, ())) == 1
    }


def slot_for_channel(
    channel: int, pins: dict[int, str], slots: dict[int, int]
) -> int | None:
    """The slot an entity on ``channel`` belongs to, or None if it must not exist.

    A channel with no identifiable door keeps its own number, unless that slot
    is pinned to a different door.
    """
    for slot, current in slots.items():
        if current == channel:
            return slot
    return None if channel in pins else channel
