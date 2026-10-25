# author: pi / worker / claude-sonnet-5-5 / thinking unknown / unknown tokens
"""pcm-tap-analyze.py sample decoding: pure logic, synthetic bytes."""
import importlib.util
from pathlib import Path
import struct
import unittest

TOOLS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pcm_tap_analyze", TOOLS / "pcm-tap-analyze.py")
analyzer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyzer)


def capture(fmt, channels, payload):
    header = bytearray(analyzer.HEADER)
    header[:8] = b"SP16PCM1"
    struct.pack_into("<IIIII", header, 12, 1, channels, fmt, 44100, analyzer.FORMATS[fmt][1] * channels)
    frames = len(payload) // (analyzer.FORMATS[fmt][1] * channels)
    struct.pack_into("<QQQQ", header, 32, frames, frames, 0, 0)
    return bytes(header) + payload


class S24DecodeTests(unittest.TestCase):
    def test_s24_le_sign_extends_from_bit_23(self):
        raw = capture(6, 1, bytes([0x00, 0x00, 0xC0, 0x00]))
        channel = analyzer.analyse(raw)["channels"][0]
        self.assertEqual(analyzer.decode(analyzer.FORMATS[6][0], "S24_LE", raw[analyzer.HEADER:], 1), (-4194304,))
        self.assertAlmostEqual(channel["peak"], 0.5)

    def test_s24_le_positive_and_full_scale_negative(self):
        data = bytes([0x00, 0x00, 0x40, 0x00, 0x00, 0x00, 0x80, 0x00])
        self.assertEqual(analyzer.decode(analyzer.FORMATS[6][0], "S24_LE", data, 2), (4194304, -8388608))


if __name__ == "__main__":
    unittest.main()
