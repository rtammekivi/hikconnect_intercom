import json
import unittest
from unittest.mock import Mock

from tests._modules import load_module

api = load_module("hikconnect_api")
doors = load_module("doors")

LOBBY, PARKING, SUB = "192.0.2.11", "192.0.2.15", "192.0.2.12"


def _door(ip, name="door"):
    return {"name": name, "ip": ip, "locks": 1}


class DoorPinningTests(unittest.TestCase):
    def test_first_sighting_pins_each_door_to_its_channel(self):
        found = {2: _door(LOBBY), 5: _door(PARKING)}
        pins = doors.pin_doors({}, found)
        self.assertEqual(pins, {2: LOBBY, 5: PARKING})
        self.assertEqual(doors.resolve_slots(pins, found), {2: 2, 5: 5})

    def test_reslotted_door_keeps_its_slot(self):
        pins = {2: LOBBY, 5: PARKING}
        found = {2: _door(PARKING)}
        self.assertEqual(doors.pin_doors(pins, found), pins)
        slots = doors.resolve_slots(pins, found)
        self.assertEqual(slots, {5: 2})
        self.assertEqual(doors.slot_for_channel(2, pins, slots), 5)

    def test_offline_door_keeps_its_own_channel(self):
        pins = {2: LOBBY, 5: PARKING}
        slots = doors.keep_absent(pins, doors.resolve_slots(pins, {5: _door(PARKING)}))
        self.assertEqual(slots, {2: 2, 5: 5})
        self.assertEqual(doors.slot_for_channel(2, pins, slots), 2)

    def test_offline_door_yields_its_channel_to_a_reslotted_door(self):
        pins = {2: LOBBY, 5: PARKING}
        slots = doors.keep_absent(pins, doors.resolve_slots(pins, {2: _door(PARKING)}))
        self.assertEqual(slots, {5: 2})
        self.assertIsNone(doors.slot_for_channel(5, pins, slots))

    def test_door_dropping_out_is_not_a_move(self):
        pins, slots = {1: LOBBY, 2: SUB}, {1: 1, 2: 2}
        self.assertFalse(doors.doors_moved(pins, slots, {1: _door(LOBBY)}))
        self.assertFalse(doors.doors_moved(pins, slots, {1: _door(LOBBY), 2: _door(None)}))
        self.assertFalse(doors.doors_moved(pins, slots, {1: _door(LOBBY), 2: _door(SUB)}))

    def test_door_on_another_channel_or_new_door_is_a_move(self):
        pins, slots = {1: LOBBY, 2: SUB}, {1: 1, 2: 2}
        self.assertTrue(doors.doors_moved(pins, slots, {1: _door(SUB), 2: _door(LOBBY)}))
        self.assertTrue(doors.doors_moved(pins, slots, {1: _door(LOBBY), 3: _door(SUB)}))
        self.assertTrue(doors.doors_moved(pins, slots, {1: _door(LOBBY), 2: _door(PARKING)}))

    def test_door_absent_at_setup_returning_elsewhere_is_a_move(self):
        pins = {2: LOBBY, 5: PARKING}
        slots = doors.keep_absent(pins, {5: 2})
        self.assertTrue(doors.doors_moved(pins, slots, {2: _door(PARKING), 3: _door(LOBBY)}))

    def test_new_door_on_a_pinned_channel_gets_a_fresh_slot(self):
        pins = {2: LOBBY, 5: PARKING}
        found = {2: _door(SUB), 5: _door(PARKING)}
        self.assertEqual(doors.pin_doors(pins, found), {2: LOBBY, 5: PARKING, 6: SUB})

    def test_duplicate_or_missing_ip_is_never_pinned(self):
        found = {1: _door(LOBBY), 2: _door(LOBBY), 3: _door(None)}
        pins = doors.pin_doors({}, found)
        self.assertEqual(pins, {})
        self.assertEqual(doors.resolve_slots({1: LOBBY}, found), {})
        self.assertEqual(doors.slot_for_channel(3, pins, {}), 3)


class DoorPresenceTests(unittest.TestCase):
    def test_door_goes_offline_only_after_tolerated_misses(self):
        presence = doors.DoorPresence({1: LOBBY, 2: SUB}, {1: _door(LOBBY), 2: _door(SUB)}, 2)
        for _ in range(2):
            presence.update({1: _door(LOBBY)})
            self.assertTrue(presence.online(2))
        presence.update({1: _door(LOBBY), 2: _door(None)})
        self.assertFalse(presence.online(2))
        self.assertTrue(presence.online(1))

    def test_door_back_resets_the_count(self):
        presence = doors.DoorPresence({1: LOBBY, 2: SUB}, {1: _door(LOBBY)}, 2)
        presence.update({1: _door(LOBBY)})
        presence.update({1: _door(LOBBY), 2: _door(SUB)})
        presence.update({1: _door(LOBBY)})
        presence.update({1: _door(LOBBY)})
        self.assertTrue(presence.online(2))

    def test_same_list_again_is_not_another_miss(self):
        stale = {1: _door(LOBBY)}
        presence = doors.DoorPresence({1: LOBBY, 2: SUB}, stale, 2)
        for _ in range(5):
            presence.update(stale)
        self.assertTrue(presence.online(2))

    def test_unpinned_slot_is_always_online(self):
        presence = doors.DoorPresence({1: LOBBY}, {}, 0)
        self.assertTrue(presence.online(3))


class DoorStationListTests(unittest.TestCase):
    def test_parses_watch_point_list(self):
        client = api.HikConnectClient("test-account", "test-password")
        client.isapi = Mock(return_value={"data": json.dumps({"WatchPoint": {"deviveList": [
            {"channelNo": 1, "unitType": "outdoor", "deviceName": "Main Door Station",
             "lockNum": 1, "ipV4Address": "192.168.8.1"},
            {"channelNo": 2, "unitType": "outdoor", "deviceName": "Sub Door Station 1",
             "lockNum": 1, "ipV4Address": "0.0.0.0"},
        ]}})})
        self.assertEqual(client.get_door_stations("TEST_STATION"), {
            1: {"name": "Main Door Station", "ip": "192.168.8.1", "locks": 1},
            2: {"name": "Sub Door Station 1", "ip": None, "locks": 1},
        })
        client.isapi.assert_called_once_with(
            "TEST_STATION", "GET", "/ISAPI/VideoIntercom/WatchPoint?format=json"
        )

    def test_unsupported_station_raises(self):
        client = api.HikConnectClient("test-account", "test-password")
        client.isapi = Mock(return_value={"data": json.dumps({"subStatusCode": "notSupport"})})
        with self.assertRaises(api.HikConnectError):
            client.get_door_stations("TEST_STATION")


if __name__ == "__main__":
    unittest.main()
