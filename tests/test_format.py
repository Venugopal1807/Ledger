"""Tests for the on-disk format primitives (DESIGN.md sections 4-7).

These tests pin the byte layout deliberately: several assert against
hard-coded hex rather than re-deriving the bytes from ledger's own struct
strings, so an accidental change to the format is a test failure rather
than a silently different file.
"""

import unittest

import ledger


def flip_bit(data: bytes, byte_index: int, bit: int) -> bytes:
    out = bytearray(data)
    out[byte_index] ^= 1 << bit
    return bytes(out)


class TestConstants(unittest.TestCase):
    def test_header_sizes_are_32_bytes(self):
        self.assertEqual(ledger.FILE_HEADER_SIZE, 32)
        self.assertEqual(ledger.RECORD_HEADER_SIZE, 32)

    def test_struct_layouts_match_declared_sizes(self):
        import struct

        self.assertEqual(
            struct.calcsize("<8sHHI12s") + struct.calcsize("<I"),
            ledger.FILE_HEADER_SIZE,
        )
        self.assertEqual(
            struct.calcsize("<4sBBHQIII") + struct.calcsize("<I"),
            ledger.RECORD_HEADER_SIZE,
        )

    def test_magics(self):
        self.assertEqual(ledger.FILE_MAGIC, b"LEDGERv1")
        self.assertEqual(len(ledger.FILE_MAGIC), 8)
        self.assertEqual(ledger.RECORD_MAGIC, b"LGR\x1e")
        self.assertEqual(len(ledger.RECORD_MAGIC), 4)

    def test_opcodes_are_distinct_and_nonzero(self):
        # Zero is excluded so a run of zero bytes cannot decode as a valid op.
        self.assertEqual(ledger.OP_PUT, 1)
        self.assertEqual(ledger.OP_DELETE, 2)

    def test_limits(self):
        self.assertEqual(ledger.MAX_KEY_BYTES, 4096)
        self.assertEqual(ledger.MAX_VALUE_BYTES, 8 * 1024 * 1024)
        self.assertEqual(ledger.FIRST_SEQ, 1)


class TestCrc32(unittest.TestCase):
    def test_known_vectors(self):
        self.assertEqual(ledger.crc32(b""), 0)
        # The standard CRC-32 check value for the ASCII string "123456789".
        self.assertEqual(ledger.crc32(b"123456789"), 0xCBF43926)

    def test_result_is_unsigned_u32(self):
        for payload in (b"", b"\xff" * 64, b"ledger", bytes(range(256))):
            value = ledger.crc32(payload)
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 0xFFFFFFFF)


class TestFileHeader(unittest.TestCase):
    def test_golden_bytes(self):
        expected = bytes.fromhex(
            "4c45444745527631"  # magic  "LEDGERv1"
            "0100"              # version 1
            "0000"              # flags 0
            "00000000"          # generation 0
            "000000000000000000000000"  # 12 reserved zero bytes
            "f902bd48"          # header crc
        )
        self.assertEqual(ledger.encode_file_header(0), expected)

    def test_size_and_magic_position(self):
        header = ledger.encode_file_header(0)
        self.assertEqual(len(header), 32)
        self.assertEqual(header[:8], ledger.FILE_MAGIC)

    def test_roundtrip(self):
        for generation in (0, 1, 7, 0xFFFFFFFF):
            decoded = ledger.decode_file_header(
                ledger.encode_file_header(generation)
            )
            self.assertEqual(decoded.generation, generation)
            self.assertEqual(decoded.version, ledger.FORMAT_VERSION)

    def test_generation_out_of_range(self):
        for bad in (-1, 2**32):
            with self.assertRaises(ValueError):
                ledger.encode_file_header(bad)

    def test_wrong_length_rejected(self):
        header = ledger.encode_file_header(0)
        for length in (0, 1, 31, 33):
            with self.assertRaises(ledger.FormatError):
                ledger.decode_file_header(header[:length].ljust(length, b"\x00"))

    def test_bad_magic(self):
        bad = b"NOTALDGR" + ledger.encode_file_header(0)[8:]
        with self.assertRaises(ledger.FormatError):
            ledger.decode_file_header(bad)

    def test_unsupported_version_is_reported_as_version(self):
        # Re-checksum after editing, so the version check is what fires.
        import struct

        prefix = bytearray(ledger.encode_file_header(0)[:28])
        struct.pack_into("<H", prefix, 8, 2)
        bad = bytes(prefix) + struct.pack("<I", ledger.crc32(bytes(prefix)))
        with self.assertRaises(ledger.FormatError) as caught:
            ledger.decode_file_header(bad)
        self.assertIn("version", str(caught.exception))

    def test_nonzero_flags_rejected(self):
        import struct

        prefix = bytearray(ledger.encode_file_header(0)[:28])
        struct.pack_into("<H", prefix, 10, 1)
        bad = bytes(prefix) + struct.pack("<I", ledger.crc32(bytes(prefix)))
        with self.assertRaises(ledger.FormatError):
            ledger.decode_file_header(bad)

    def test_nonzero_reserved_rejected(self):
        import struct

        prefix = bytearray(ledger.encode_file_header(0)[:28])
        prefix[16] = 0x01
        bad = bytes(prefix) + struct.pack("<I", ledger.crc32(bytes(prefix)))
        with self.assertRaises(ledger.FormatError):
            ledger.decode_file_header(bad)

    def test_every_single_bit_flip_is_detected(self):
        header = ledger.encode_file_header(3)
        self.assertEqual(len(header), 32)
        for index in range(32):
            for bit in range(8):
                with self.subTest(byte=index, bit=bit):
                    with self.assertRaises(ledger.FormatError):
                        ledger.decode_file_header(flip_bit(header, index, bit))


class TestRecordEncoding(unittest.TestCase):
    def test_golden_put_bytes(self):
        expected = bytes.fromhex(
            "4c47521e"          # magic  "LGR\x1e"
            "01"                # version 1
            "01"                # op = PUT
            "0000"              # flags 0
            "0100000000000000"  # seq 1
            "07000000"          # key_len 7
            "0f000000"          # val_len 15
            "9492a57a"          # payload crc
            "98eb988f"          # header crc
            "757365723a3432"    # b"user:42"
            "7b226e616d65223a2256656e75227d"  # b'{"name":"Venu"}'
        )
        actual = ledger.encode_record(
            ledger.OP_PUT, 1, b"user:42", b'{"name":"Venu"}'
        )
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 32 + 7 + 15)

    def test_golden_delete_bytes(self):
        expected = bytes.fromhex(
            "4c47521e"
            "01"
            "02"                # op = DELETE
            "0000"
            "0200000000000000"  # seq 2
            "07000000"          # key_len 7
            "00000000"          # val_len 0
            "860d6f64"
            "bdefb148"
            "757365723a3432"
        )
        actual = ledger.encode_record(ledger.OP_DELETE, 2, b"user:42")
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 32 + 7)

    def test_roundtrip_put(self):
        key, value = b"user:42", b'{"name":"Venu"}'
        raw = ledger.encode_record(ledger.OP_PUT, 9, key, value)
        header = ledger.decode_record_header(raw[:32])
        self.assertEqual(header.op, ledger.OP_PUT)
        self.assertEqual(header.seq, 9)
        self.assertEqual(header.key_len, len(key))
        self.assertEqual(header.val_len, len(value))
        self.assertEqual(header.payload_len, len(key) + len(value))
        self.assertEqual(header.total_size, len(raw))
        payload = raw[32:]
        ledger.verify_payload(header, payload)
        self.assertEqual(ledger.split_payload(header, payload), (key, value))

    def test_roundtrip_delete(self):
        key = b"user:42"
        raw = ledger.encode_record(ledger.OP_DELETE, 4, key)
        header = ledger.decode_record_header(raw[:32])
        self.assertEqual(header.op, ledger.OP_DELETE)
        self.assertEqual(header.val_len, 0)
        self.assertEqual(header.total_size, len(raw))
        payload = raw[32:]
        ledger.verify_payload(header, payload)
        self.assertEqual(ledger.split_payload(header, payload), (key, b""))

    def test_roundtrip_boundary_sizes(self):
        cases = [
            (b"k", b"0"),
            (b"k" * ledger.MAX_KEY_BYTES, b"0"),
            (b"k", b"v" * 1024),
            (b"\xff\x00\xfe", b'"\\u0000"'),
            ("ключ".encode(), '"значение"'.encode()),
        ]
        for key, value in cases:
            with self.subTest(key_len=len(key), val_len=len(value)):
                raw = ledger.encode_record(ledger.OP_PUT, 1, key, value)
                header = ledger.decode_record_header(raw[:32])
                payload = raw[32:]
                ledger.verify_payload(header, payload)
                self.assertEqual(
                    ledger.split_payload(header, payload), (key, value)
                )

    def test_payload_containing_record_magic_is_unambiguous(self):
        # Framing is length-prefixed, so the magic may appear inside a value.
        value = b'"' + ledger.RECORD_MAGIC + b'"'
        raw = ledger.encode_record(ledger.OP_PUT, 1, ledger.RECORD_MAGIC, value)
        header = ledger.decode_record_header(raw[:32])
        payload = raw[32:]
        ledger.verify_payload(header, payload)
        self.assertEqual(
            ledger.split_payload(header, payload), (ledger.RECORD_MAGIC, value)
        )

    def test_sequence_numbers_span_u64(self):
        for seq in (1, 2, 2**32, ledger.MAX_SEQ):
            raw = ledger.encode_record(ledger.OP_PUT, seq, b"k", b"1")
            self.assertEqual(ledger.decode_record_header(raw[:32]).seq, seq)

    def test_rejects_bad_sequence_numbers(self):
        for seq in (0, -1, 2**64):
            with self.subTest(seq=seq), self.assertRaises(ValueError):
                ledger.encode_record(ledger.OP_PUT, seq, b"k", b"1")

    def test_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            ledger.encode_record(ledger.OP_PUT, 1, "k", b"1")
        with self.assertRaises(TypeError):
            ledger.encode_record(ledger.OP_PUT, 1, b"k", "1")

    def test_rejects_invalid_shapes(self):
        cases = [
            ("empty key", ledger.OP_PUT, b"", b"1", ledger.REASON_INVALID_KEY_LEN),
            (
                "oversize key",
                ledger.OP_PUT,
                b"k" * (ledger.MAX_KEY_BYTES + 1),
                b"1",
                ledger.REASON_INVALID_KEY_LEN,
            ),
            (
                "empty put value",
                ledger.OP_PUT,
                b"k",
                b"",
                ledger.REASON_INVALID_VAL_LEN,
            ),
            (
                "oversize value",
                ledger.OP_PUT,
                b"k",
                b"v" * (ledger.MAX_VALUE_BYTES + 1),
                ledger.REASON_INVALID_VAL_LEN,
            ),
            (
                "delete with value",
                ledger.OP_DELETE,
                b"k",
                b"1",
                ledger.REASON_INVALID_VAL_LEN,
            ),
            ("unknown op", 0, b"k", b"1", ledger.REASON_INVALID_OP),
            ("unknown op high", 99, b"k", b"1", ledger.REASON_INVALID_OP),
        ]
        for name, op, key, value, _reason in cases:
            with self.subTest(name):
                # A bad argument is a caller bug, so this is a plain
                # ValueError; the same bytes read back off disk decode as
                # CorruptRecordError instead (see TestRecordDecoding).
                with self.assertRaises(ValueError):
                    ledger.encode_record(op, 1, key, value)


class TestRecordDecoding(unittest.TestCase):
    def setUp(self):
        self.raw = ledger.encode_record(
            ledger.OP_PUT, 1, b"user:42", b'{"name":"Venu"}'
        )
        self.header_bytes = self.raw[:32]
        self.payload = self.raw[32:]

    def _rechecksum(self, header: bytes) -> bytes:
        """Rebuild a header's checksum so a field check fires, not the CRC."""
        import struct

        prefix = header[:28]
        return prefix + struct.pack("<I", ledger.crc32(prefix))

    def _edit(self, offset: int, packed: bytes) -> bytes:
        out = bytearray(self.header_bytes)
        out[offset : offset + len(packed)] = packed
        return self._rechecksum(bytes(out))

    def test_wrong_header_length_is_a_programming_error(self):
        # A short read is a torn tail; classifying it belongs to the reader,
        # so this layer refuses the call rather than guessing.
        short_cases = [self.header_bytes[:n] for n in (0, 1, 16, 31)]
        long_cases = [self.header_bytes + b"\x00", self.raw]
        for buf in short_cases + long_cases:
            with self.subTest(length=len(buf)):
                with self.assertRaises(ValueError):
                    ledger.decode_record_header(buf)

    def test_header_checksum_mismatch(self):
        bad = bytearray(self.header_bytes)
        bad[28] ^= 0xFF
        with self.assertRaises(ledger.CorruptRecordError) as caught:
            ledger.decode_record_header(bytes(bad))
        self.assertEqual(caught.exception.reason, ledger.REASON_HEADER_CRC)

    def test_bad_magic(self):
        bad = self._edit(0, b"XXXX")
        with self.assertRaises(ledger.CorruptRecordError) as caught:
            ledger.decode_record_header(bad)
        self.assertEqual(caught.exception.reason, ledger.REASON_BAD_MAGIC)

    def test_bad_version(self):
        bad = self._edit(4, bytes([2]))
        with self.assertRaises(ledger.CorruptRecordError) as caught:
            ledger.decode_record_header(bad)
        self.assertEqual(caught.exception.reason, ledger.REASON_BAD_VERSION)

    def test_unknown_operation(self):
        for op in (0, 3, 99, 255):
            with self.subTest(op=op):
                bad = self._edit(5, bytes([op]))
                with self.assertRaises(ledger.CorruptRecordError) as caught:
                    ledger.decode_record_header(bad)
                self.assertEqual(caught.exception.reason, ledger.REASON_INVALID_OP)

    def test_nonzero_flags(self):
        import struct

        bad = self._edit(6, struct.pack("<H", 1))
        with self.assertRaises(ledger.CorruptRecordError) as caught:
            ledger.decode_record_header(bad)
        self.assertEqual(caught.exception.reason, ledger.REASON_INVALID_FLAGS)

    def test_invalid_key_lengths(self):
        import struct

        for key_len in (0, ledger.MAX_KEY_BYTES + 1, 0xFFFFFFFF):
            with self.subTest(key_len=key_len):
                bad = self._edit(16, struct.pack("<I", key_len))
                with self.assertRaises(ledger.CorruptRecordError) as caught:
                    ledger.decode_record_header(bad)
                self.assertEqual(
                    caught.exception.reason, ledger.REASON_INVALID_KEY_LEN
                )

    def test_invalid_value_lengths(self):
        import struct

        for val_len in (0, ledger.MAX_VALUE_BYTES + 1, 0xFFFFFFFF):
            with self.subTest(val_len=val_len):
                bad = self._edit(20, struct.pack("<I", val_len))
                with self.assertRaises(ledger.CorruptRecordError) as caught:
                    ledger.decode_record_header(bad)
                self.assertEqual(
                    caught.exception.reason, ledger.REASON_INVALID_VAL_LEN
                )

    def test_absurd_length_is_rejected_without_allocating(self):
        # The point of the header checksum: a 4 GiB length never reaches a
        # read() call because the header is validated first.
        import struct

        bad = self._edit(20, struct.pack("<I", 0xFFFFFFFF))
        with self.assertRaises(ledger.CorruptRecordError):
            ledger.decode_record_header(bad)

    def test_delete_with_nonzero_value_length(self):
        import struct

        delete = bytearray(ledger.encode_record(ledger.OP_DELETE, 1, b"k")[:32])
        delete[20:24] = struct.pack("<I", 5)
        bad = self._rechecksum(bytes(delete))
        with self.assertRaises(ledger.CorruptRecordError) as caught:
            ledger.decode_record_header(bad)
        self.assertEqual(caught.exception.reason, ledger.REASON_INVALID_VAL_LEN)

    def test_every_single_bit_flip_in_header_is_detected(self):
        for index in range(32):
            for bit in range(8):
                with self.subTest(byte=index, bit=bit):
                    with self.assertRaises(ledger.CorruptRecordError):
                        ledger.decode_record_header(
                            flip_bit(self.header_bytes, index, bit)
                        )

    def test_every_single_bit_flip_in_payload_is_detected(self):
        header = ledger.decode_record_header(self.header_bytes)
        for index in range(len(self.payload)):
            for bit in range(8):
                with self.subTest(byte=index, bit=bit):
                    with self.assertRaises(ledger.CorruptRecordError) as caught:
                        ledger.verify_payload(
                            header, flip_bit(self.payload, index, bit)
                        )
                    self.assertEqual(
                        caught.exception.reason, ledger.REASON_PAYLOAD_CRC
                    )

    def test_verify_payload_accepts_correct_payload(self):
        header = ledger.decode_record_header(self.header_bytes)
        self.assertIsNone(ledger.verify_payload(header, self.payload))

    def test_verify_payload_rejects_wrong_length(self):
        header = ledger.decode_record_header(self.header_bytes)
        for payload in (b"", self.payload[:-1], self.payload + b"x"):
            with self.subTest(length=len(payload)):
                with self.assertRaises(ValueError):
                    ledger.verify_payload(header, payload)

    def test_split_payload_rejects_wrong_length(self):
        header = ledger.decode_record_header(self.header_bytes)
        with self.assertRaises(ValueError):
            ledger.split_payload(header, self.payload[:-1])


class TestErrorHierarchy(unittest.TestCase):
    def test_all_errors_derive_from_ledger_error(self):
        self.assertTrue(issubclass(ledger.FormatError, ledger.LedgerError))
        self.assertTrue(issubclass(ledger.CorruptRecordError, ledger.LedgerError))

    def test_corrupt_record_error_carries_reason(self):
        error = ledger.CorruptRecordError(ledger.REASON_HEADER_CRC, "boom")
        self.assertEqual(error.reason, "header_crc")
        self.assertEqual(str(error), "boom")


if __name__ == "__main__":
    unittest.main()
