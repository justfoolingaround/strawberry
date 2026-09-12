def checked_add(integer: int, quantity: int, limit: int):
    return (integer + quantity) % limit


def partition_chunks(data: bytes, chunk_size: int):
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]
