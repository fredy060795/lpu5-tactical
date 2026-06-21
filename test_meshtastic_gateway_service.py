#!/usr/bin/env python3

import zlib
import tempfile
import unittest

from meshtastic_gateway_service import (
    MESHTASTIC_ATAK_FORWARDER_PORTNUM,
    MESHTASTIC_TRANSFER_TYPE_COT,
    MeshtasticGatewayService,
)


class _FakeInterface:
    def __init__(self):
        self.nodes = {}
        self.data_calls = []
        self.text_calls = []

    def sendData(self, payload, **kwargs):
        self.data_calls.append((payload, kwargs))

    def sendText(self, text):
        self.text_calls.append(text)


class _TextOnlyInterface:
    def __init__(self):
        self.nodes = {}
        self.text_calls = []

    def sendText(self, text):
        self.text_calls.append(text)


class TestMeshtasticGatewayService(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.events = []
        self.service = MeshtasticGatewayService(
            "COM1",
            base_path=self.tmpdir.name,
            broadcast_callback=lambda event_type, data: self.events.append((event_type, data)),
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_encode_decode_cot_payload_round_trip(self):
        cot_xml = '<event version="2.0" uid="mesh-1" type="a-f-G-E-S-U-M"><point lat="1" lon="2"/></event>'
        payload = self.service.encode_cot_payload(cot_xml)

        self.assertEqual(payload[0], MESHTASTIC_TRANSFER_TYPE_COT)
        self.assertEqual(zlib.decompress(payload[1:]).decode("utf-8"), cot_xml)
        self.assertEqual(self.service.decode_cot_payload(payload), cot_xml)

    def test_decode_accepts_unprefixed_payload(self):
        cot_xml = '<event version="2.0" uid="mesh-2" type="a-f-G-E-S-U-M"><point lat="1" lon="2"/></event>'
        payload = self.service.encode_cot_payload(cot_xml)[1:]

        self.assertEqual(self.service.decode_cot_payload(payload), cot_xml)

    def test_process_message_routes_cot_text_to_gateway_cot(self):
        packet = {
            "from": "!abcd1234",
            "decoded": {
                "text": '<event version="2.0" uid="mesh-3" type="a-f-G-E-S-U-M"><point lat="1" lon="2"/></event>'
            },
        }

        self.service.process_message(packet)

        self.assertEqual(len(self.events), 1)
        event_type, payload = self.events[0]
        self.assertEqual(event_type, "gateway_cot")
        self.assertIn('uid="mesh-3"', payload["xml"])

    def test_on_receive_packet_routes_binary_cot_payload(self):
        cot_xml = '<event version="2.0" uid="mesh-4" type="a-f-G-E-S-U-M"><point lat="1" lon="2"/></event>'
        packet = {
            "id": 42,
            "from": "!abcd1234",
            "decoded": {
                "payload": self.service.encode_cot_payload(cot_xml),
                "portnum": "ATAK_FORWARDER",
            },
        }

        self.service.on_receive_packet(packet, None)

        self.assertEqual(len(self.events), 1)
        event_type, payload = self.events[0]
        self.assertEqual(event_type, "gateway_cot")
        self.assertEqual(payload["packet_id"], 42)
        self.assertEqual(payload["from"], "!abcd1234")

    def test_send_cot_uses_senddata_forwarder_port(self):
        cot_xml = '<event version="2.0" uid="mesh-5" type="a-f-G-E-S-U-M"><point lat="1" lon="2"/></event>'
        fake_interface = _FakeInterface()
        self.service.interface = fake_interface

        transport = self.service.send_cot(cot_xml)

        self.assertEqual(transport, "ATAK_FORWARDER")
        self.assertEqual(len(fake_interface.data_calls), 1)
        payload, kwargs = fake_interface.data_calls[0]
        self.assertEqual(payload[0], MESHTASTIC_TRANSFER_TYPE_COT)
        self.assertEqual(kwargs.get("portNum"), MESHTASTIC_ATAK_FORWARDER_PORTNUM)
        self.assertEqual(fake_interface.text_calls, [])

    def test_send_cot_falls_back_to_text_when_senddata_is_missing(self):
        cot_xml = '<event version="2.0" uid="mesh-6" type="a-f-G-E-S-U-M"><point lat="1" lon="2"/></event>'
        text_only_interface = _TextOnlyInterface()
        self.service.interface = text_only_interface

        transport = self.service.send_cot(cot_xml)

        self.assertEqual(transport, "TEXT_FALLBACK")
        self.assertEqual(text_only_interface.text_calls, [cot_xml])


if __name__ == "__main__":
    unittest.main()
