import base64
import os
import random
import time

import google_crc32c
import pytest

from app.storage.crc32c import combine_parts, decode, encode


def b64(data):
    return base64.b64encode(google_crc32c.Checksum(data).digest()).decode()


@pytest.mark.parametrize("seed", range(10))
def test_parts_combine_into_the_whole_object_checksum(seed):
    rng = random.Random(seed)
    pieces = [rng.randbytes(rng.randrange(1, 5000)) for _ in range(rng.randrange(1, 6))]
    assert combine_parts([(b64(piece), len(piece)) for piece in pieces]) == b64(b"".join(pieces))


def test_no_parts_is_the_checksum_of_nothing():
    assert combine_parts([]) == b64(b"")


def test_ten_thousand_parts_combine_quickly():
    part = os.urandom(1024)
    started = time.monotonic()
    combine_parts([(b64(part), len(part))] * 10000)
    assert time.monotonic() - started < 1


def test_base64_round_trip_and_strict_decoding():
    assert decode(encode(0x12345678)) == 0x12345678
    with pytest.raises(ValueError):
        decode("not base64!")
