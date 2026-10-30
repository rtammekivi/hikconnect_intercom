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


def keep_absent(pins: dict[int, str], slots: dict[int, int]) -> dict[int, int]:
    """Give each pinned door missing from ``slots`` its own slot's channel, unless taken."""
    taken = set(slots.values())
    return {**slots, **{slot: slot for slot in pins if slot not in slots and slot not in taken}}


def doors_moved(pins: dict[int, str], slots: dict[int, int], doors: dict[int, dict]) -> bool:
    """True if a new door appeared or a present door is no longer on its slot's channel."""
    if pin_doors(pins, doors) != pins:
        return True
    return any(slots.get(slot) != channel for slot, channel in resolve_slots(pins, doors).items())


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


class DoorPresence:
    """Consecutive door lists each pinned door has been missing from."""

    def __init__(self, pins: dict[int, str], doors: dict[int, dict], tolerance: int) -> None:
        self._pins = pins
        self._tolerance = tolerance
        self._misses: dict[int, int] = {}
        self._last: dict[int, dict] | None = None
        self.update(doors)

    def update(self, doors: dict[int, dict]) -> None:
        if doors is self._last:
            return
        self._last = doors
        present = resolve_slots(self._pins, doors)
        self._misses = {
            slot: 0 if slot in present else self._misses.get(slot, 0) + 1 for slot in self._pins
        }

    def online(self, slot: int) -> bool:
        return self._misses.get(slot, 0) <= self._tolerance
