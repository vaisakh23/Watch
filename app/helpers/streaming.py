import re


def parse_range(header, size):
    """Return (start, end) inclusive, None if no Range header, or 'bad'."""
    if not header:
        return None
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip(), re.IGNORECASE)
    if not m:
        return "bad"
    s, e = m.groups()
    if s == "" and e == "":
        return "bad"
    try:
        if s == "":
            suffix_length = int(e)
            if suffix_length <= 0:
                return "bad"
            start, end = max(size - suffix_length, 0), size - 1
        else:
            start = int(s)
            end = min(int(e), size - 1) if e else size - 1
    except ValueError:
        return "bad"
    if start < 0 or start > end or start >= size:
        return "bad"
    return start, end
