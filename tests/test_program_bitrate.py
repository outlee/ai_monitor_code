# -*- coding: utf-8 -*-
"""节目码率按 PAT/PMT 拆开，不把整路组播算到每一个节目上。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))

from iface_mcast import (  # noqa: E402
    _NEED_REASM,
    _PayloadQueue,
    _ProgramMeter,
    _TsRing,
    _close_ring,
    _deliver,
    _open_shared_ring,
    _stats_for_json,
    _udp_from_frame,
    pick_program_bitrate,
    pick_program_codec,
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


def _pmt_es(program, pcr, items, version=0):
    es = bytearray()
    for stype, epid, desc in items:
        desc = desc or b""
        es.extend(
            bytes(
                (
                    stype & 0xFF,
                    0xE0 | ((epid >> 8) & 0x1F),
                    epid & 0xFF,
                    0xF0 | ((len(desc) >> 8) & 0x0F),
                    len(desc) & 0xFF,
                )
            )
        )
        es.extend(desc)
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
        self.assertEqual(stats["codecs"][101]["video"], "H.264")
        self.assertEqual(stats["codecs"][101]["audio"], "")
        self.assertIsInstance(stats["programs"][101], float)

    def test_pmt_records_video_and_audio_codec(self):
        ac3 = bytes((0x6A, 0x01, 0x00))
        dra = bytes((0x05, 0x04)) + b"DRA1"
        meter = _ProgramMeter()
        meter.feed(
            b"".join(
                [
                    _psi_packet(0, _pat([(201, 300), (202, 301), (203, 302)])),
                    _psi_packet(
                        300,
                        _pmt_es(
                            201,
                            410,
                            [(0x02, 410, b""), (0x04, 411, b""), (0x06, 412, ac3)],
                        ),
                    ),
                    _psi_packet(
                        301,
                        _pmt_es(202, 510, [(0x1B, 510, b""), (0x0F, 511, b"")]),
                    ),
                    _psi_packet(
                        302,
                        _pmt_es(203, 610, [(0x24, 610, b""), (0x06, 611, dra)]),
                    ),
                ]
            )
        )
        self.assertEqual(meter.codecs[201], {"video": "MPEG-2", "audio": "MP2/AC3"})
        self.assertEqual(meter.codecs[202], {"video": "H.264", "audio": "AAC"})
        self.assertEqual(meter.codecs[203], {"video": "H.265", "audio": "DRA"})
        self.assertEqual(meter.pids[201], {300, 410, 411, 412})
        self.assertEqual(pick_program_codec(meter.codecs, 201)["audio"], "MP2/AC3")
        self.assertIsNone(pick_program_codec(meter.codecs, None))
        self.assertEqual(
            pick_program_codec({7: {"video": "H.265", "audio": "AAC"}}, None)["video"],
            "H.265",
        )
        self.assertEqual(pick_program_codec({"202": meter.codecs[202]}, 202)["video"], "H.264")
        meter.feed(_psi_packet(0, _pat([(202, 301)])))
        self.assertNotIn(201, meter.codecs)
        self.assertEqual(meter.codecs[202]["audio"], "AAC")

    def test_snapshot_keeps_every_chunk_already_written(self):
        ring = _TsRing(1024 * 1024)
        blob = b"\x47" + b"\x11" * 187
        for _ in range(40):
            ring.write(blob)
        snap = ring.snapshot()
        ring.write(blob)
        self.assertEqual(len(snap), 40 * 188)
        self.assertEqual(snap[:1], b"\x47")
        self.assertEqual(ring.size(), 41 * 188)

    def test_ring_wrap_keeps_the_newest_packets_in_order(self):
        ring = _TsRing(188 * 10)
        for i in range(25):
            ring.write(bytes((0x47, i)) + b"\x00" * 186)
        snap = ring.snapshot()
        self.assertEqual(len(snap), 188 * 10)
        self.assertEqual(snap[0], 0x47)
        self.assertEqual(snap[1], 15)
        self.assertEqual(snap[9 * 188 + 1], 24)
        ring.write(memoryview(bytes((0x47, 99)) + b"\x11" * 186))
        snap2 = ring.snapshot()
        self.assertEqual(snap2[0], 0x47)
        self.assertEqual(snap2[1], 16)
        self.assertEqual(snap2[-187], 99)
        self.assertEqual(snap[1], 15)

    def test_shared_ring_second_handle_sees_the_write(self):
        fd, path = tempfile.mkstemp(prefix="amcr_test_")
        os.close(fd)
        os.unlink(path)
        writer = _open_shared_ring(path, True, nbytes=188 * 10)
        reader = _open_shared_ring(path, False, nbytes=188 * 10)
        try:
            for i in range(25):
                writer.write(bytes((0x47, i)) + b"\x00" * 186)
            snap = reader.snapshot()
            self.assertEqual(len(snap), 188 * 10)
            self.assertEqual(snap[0], 0x47)
            self.assertEqual(snap[1], 15)
            self.assertEqual(snap[9 * 188 + 1], 24)
            self.assertEqual(reader.size(), 188 * 10)
        finally:
            _close_ring(writer, False)
            _close_ring(reader, True)

    def test_shared_ring_other_process_write_is_visible(self):
        fd, path = tempfile.mkstemp(prefix="amcr_test_")
        os.close(fd)
        os.unlink(path)
        reader = _open_shared_ring(path, True, nbytes=188 * 4)
        code = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from iface_mcast import _close_ring, _open_shared_ring\n"
            "ring = _open_shared_ring(%r, False, nbytes=188 * 4)\n"
            "ring.write(bytes((0x47, 21)) + b'\\x00' * 186)\n"
            "_close_ring(ring, False)\n"
        ) % (str(ROOT / "workers"), path)
        try:
            subprocess.check_call([sys.executable, "-c", code])
            snap = reader.snapshot()
            self.assertEqual(snap[0], 0x47)
            self.assertEqual(snap[1], 21)
            self.assertEqual(reader.size(), 188)
        finally:
            _close_ring(reader, True)

    def test_stats_json_keeps_program_lookup(self):
        raw = {
            "bitrate_kbps": 34000.0,
            "programs": {301: 1500.0},
            "codecs": {301: {"video": "MPEG-2", "audio": "MP2"}},
        }
        packed = json.loads(json.dumps(_stats_for_json(raw)))
        self.assertEqual(pick_program_bitrate(packed["programs"], 301), 1500.0)
        self.assertEqual(pick_program_codec(packed["codecs"], 301)["video"], "MPEG-2")

    def test_fanout_queue_drops_oldest_copy_only(self):
        ring = _TsRing(1024 * 1024)
        q = _PayloadQueue(400)
        hub = object()
        for i in range(5):
            payload = bytes([i + 1]) * 188
            ring.write(payload)
            q.put(hub, payload)
        self.assertEqual(ring.size(), 5 * 188)
        self.assertGreater(q.dropped, 0)
        got = []
        while True:
            item = q.get(0.01)
            if item is None:
                break
            got.append(item[1][0])
        self.assertEqual(got, [4, 5])

    def test_udp_from_frame_keeps_ts_and_rejects_fragments(self):
        ts = (b"\x47" + b"\x22" * 187) * 2 + b"\x99" * 10
        frame = _udp_frame("239.100.1.67", 5000, ts)
        dst, dport, payload = _udp_from_frame(memoryview(frame))
        self.assertEqual(dst, socket.inet_aton("239.100.1.67"))
        self.assertEqual(dport, 5000)
        self.assertEqual(payload, (b"\x47" + b"\x22" * 187) * 2)
        vlan = _udp_frame("239.100.1.67", 5000, ts[:188], vlan=True)
        self.assertEqual(_udp_from_frame(vlan)[2], ts[:188])
        rtp = bytes((0x80, 0x21)) + b"\x00" * 10 + ts[:188]
        wrapped = _udp_frame("239.100.1.67", 5000, rtp)
        self.assertEqual(_udp_from_frame(wrapped)[2], ts[:188])
        frag = bytearray(_udp_frame("239.100.1.67", 5000, ts[:188]))
        frag[20] = 0x20
        self.assertIs(_udp_from_frame(bytes(frag)), _NEED_REASM)
        self.assertIsNone(_udp_from_frame(b"\x00" * 40))


def _udp_frame(dst, dport, payload, vlan=False):
    udp_len = 8 + len(payload)
    total = 20 + udp_len
    ip = bytearray(20)
    ip[0] = 0x45
    ip[2] = (total >> 8) & 0xFF
    ip[3] = total & 0xFF
    ip[9] = 17
    ip[16:20] = socket.inet_aton(dst)
    udp = bytearray(8)
    udp[2] = (dport >> 8) & 0xFF
    udp[3] = dport & 0xFF
    udp[4] = (udp_len >> 8) & 0xFF
    udp[5] = udp_len & 0xFF
    eth = bytearray(12) + (b"\x81\x00\x00\x14\x08\x00" if vlan else b"\x08\x00")
    return bytes(eth) + bytes(ip) + bytes(udp) + payload


if __name__ == "__main__":
    unittest.main()
