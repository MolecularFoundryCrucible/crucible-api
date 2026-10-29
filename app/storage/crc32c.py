import base64

from awscrt.checksums import combine_crc32c


def decode(value: str) -> int:
    return int.from_bytes(base64.b64decode(value, validate=True), "big")


def encode(value: int) -> str:
    return base64.b64encode(value.to_bytes(4, "big")).decode()


def combine_parts(parts: list[tuple[str, int]]) -> str:
    """Base64 CRC32C of the parts joined in order, from each part's base64 CRC32C and size."""
    total = 0
    for crc, size in parts:
        total = combine_crc32c(total, decode(crc), size)
    return encode(total)
