# -*- coding: utf-8 -*-
"""节目码率按 PAT/PMT 拆开，不把整路组播算到每一个节目上。"""
from __future__ import annotations

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))

from iface_mcast import (  # noqa: E402
    _ProgramMeter,
    _TsRing,
    _deliver,
    pick_program_bitrate,
)


def _pat(progs, version=0):
    loop = bytearray()
    for pnum, pid in progs:
        loop.extend(
            bytes(
                (
                    (pnum >> 8) & 0xFF,
                    pnum & 0xFF,
                    0xE0 | ((pid >> 8) & 0x1F),
                    pid & 0xFF,
                )
            )
        )
    seclen = 5 + len(loop) + 4
    return bytes(
        (
            0x00,
            0xB0 | ((seclen >> 8) & 0x0F),
            seclen & 0xFF,
            0x00,
            0x01,
            0xC1 | ((version & 0x1F) << 1),
            0x00,
            0x00,
        )
    ) + bytes(loop) + b"\x00\x00\x00\x00"


def _pmt(program, pcr, es_pids, version=0):
    es = bytearray()
    for epid in es_pids:
        es.extend(
            bytes(
                (
                    0x1B,
                    0xE0 | ((epid >> 8) & 0x1F),
                    epid & 0xFF,
                    0xF0,
                    0x00,
                )
            )
        )
    seclen = 9 + len(es) + 4
    return bytes(
        (
            0x02,
            0xB0 | ((seclen >> 8) & 0x0F),
            seclen & 0xFF,
            (program >> 8) & 0xFF,
            program & 0xFF,
            0xC1 | ((version & 0x1F) << 1),
            0x00,
            0x00,
            0xE0 | ((pcr >> 8) & 0x1F),
            pcr & 0xFF,
            0xF0,
            0x00,
        )
    ) + bytes(es) + b"\x00\x00\x00\x00"


def _psi_packet(pid, section, cc=0):
    pkt = bytearray(b"\xff" * 188)
    pkt[0] = 0x47
    pkt[1] = 0x40 | ((pid >> 8) & 0x1F)
    pkt[2] = pid & 0xFF
    pkt[3] = 0x10 | (cc & 0x0F)
    pkt[4] = 0x00
    pkt[5 : 5 + len(section)] = section
    return bytes(pkt)


def _data_packet(pid, cc=0):
    pkt = bytearray(188)
    pkt[0] = 0x47
    pkt[1] = (pid >> 8) & 0x1F
    pkt[2] = pid & 0xFF
    pkt[3] = 0x10 | (cc & 0x0F)
    return bytes(pkt)


def _kbps(packets):
    return round(packets * 188 * 8.0 / 1000.0, 1)


class ProgramBitrateTests(unittest.TestCase):
    def test_two_programs_exclude_null_and_pat(self):
        meter = _ProgramMeter()
        parts = [
            _psi_packet(0, _pat([(101, 256), (102, 257)])),
            _psi_packet(256, _pmt(101, 411, [411, 412])),
            _psi_packet(257, _pmt(102, 511, [511, 512])),
        ]
        parts.extend(_data_packet(411, i % 16) for i in range(100))
        parts.extend(_data_packet(412, i % 16) for i in range(40))
        parts.extend(_data_packet(511, i % 16) for i in range(80))
        parts.extend(_data_packet(0x1FFF) for _ in range(20))
        parts.extend(_data_packet(999) for _ in range(5))
        meter.feed(b"".join(parts))
        self.assertEqual(meter.pids[101], {256, 411, 412})
        self.assertEqual(meter.pids[102], {257, 511, 512})
        rates = meter.roll(1.0)
        self.assertEqual(rates[101], _kbps(1 + 100 + 40))
        self.assertEqual(rates[102], _kbps(1 + 80))
        self.assertNotIn(0, rates)

    def test_single_program_mux_is_that_program(self):
        meter = _ProgramMeter()
        blob = b"".join(
            [
                _psi_packet(0, _pat([(7, 32)])),
                _psi_packet(32, _pmt(7, 100, [100])),
                _data_packet(100),
                _data_packet(100),
                _data_packet(0x1FFF),
            ]
        )
        meter.feed(blob)
        rates = meter.roll(1.0)
        self.assertEqual(pick_program_bitrate(rates, None), _kbps(1 + 2))
        self.assertIsNone(pick_program_bitrate(rates, 8))

    def test_pick_waits_until_the_configured_program_is_known(self):
        self.assertIsNone(pick_program_bitrate({}, 101))
        self.assertIsNone(pick_program_bitrate({101: 10.0, 102: 20.0}, None))
        self.assertEqual(pick_program_bitrate({101: 10.0, 102: 20.0}, 102), 20.0)
        self.assertEqual(pick_program_bitrate({"101": 10.0}, 101), 10.0)
        self.assertEqual(pick_program_bitrate({101: 0.0}, 101), 0.0)
        self.assertIsNone(pick_program_bitrate({101: 10.0}, 102))

    def test_deliver_keeps_mux_total_beside_program_rates(self):
        hub = {
            "iface": "lo",
            "group": "239.1.1.1",
            "mport": 5000,
            "ports": {},
            "feeders": {},
            "dest_lock": threading.Lock(),
            "ring": _TsRing(1024 * 1024),
            "prog": _ProgramMeter(),
        }
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            first = b"".join(
                [
                    _psi_packet(0, _pat([(101, 256), (102, 257)])),
                    _psi_packet(256, _pmt(101, 411, [411])),
                    _psi_packet(257, _pmt(102, 511, [511])),
                    _data_packet(411),
                    _data_packet(0x1FFF),
                ]
            )
            _deliver(hub, first, sock)
            hub["_win"]["t"] = time.time() - 1.2
            second = _data_packet(511) + _data_packet(411)
            _deliver(hub, second, sock)
        finally:
            sock.close()
        stats = hub["stats"]
        # 同一时间窗里：101 有 3 个包，102 有 2 个，整路还有 PAT 和空包。
        self.assertGreater(stats["programs"][101], stats["programs"][102])
        self.assertGreater(
            stats["bitrate_kbps"],
            stats["programs"][101] + stats["programs"][102],
        )


if __name__ == "__main__":
    unittest.main()
