"""Filename parsing and title-key helpers."""

import re

# ------------------------ filename parsing ------------------------

VIDEO_EXT = (
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".webm",
    ".m4v",
    ".ts",
    ".flv",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".mp3",
    ".m4a",
    ".flac",
    ".ogg",
    ".opus",
    ".wav",
)

QUALITY = re.compile(
    r"(?i)\b(480p|576p|720p|1080p|1440p|2160p|4k|uhd|blu[ -]?ray|brrip|bdrip|web[ -]?dl|web[ -]?rip|"
    r"hdrip|hdtv|dvdrip|hdcam|x26[45]|h[ .]?26[45]|hevc|10[ -]?bit|remux|proper|repack|"
    r"extended|unrated|pahe|yify|yts|rarbg)\b"
)


def clean(s):
    s = re.sub(
        r"(?i)\.(mkv|mp4|avi|mov|webm|m4v|ts|flv|wmv|mpg|mpeg|mp3|m4a|flac|ogg|opus|wav)$",
        "",
        s.strip(),
    )
    for _ in range(3):  # leading [Group] or (tag)
        s2 = re.sub(r"^\s*(\[[^\]]*\]|\([^)]*\))\s*", "", s)
        if s2 == s:
            break
        s = s2
    s = re.sub(
        r"(?i)^(www\.)?[\w-]+\.(com|net|org|in|to|io|cc|xyz|me|tv)\s*[-\u2013_ ]+",
        "",
        s,
    )
    s = re.sub(r"[._]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def finish_title(t):
    t = re.sub(r"[\s\-\u2013:(\[]+$", "", t.strip())
    t = re.sub(r"^[\s\-\u2013:)\]]+", "", t)
    if t and (t.islower() or t.isupper()):
        t = t.title()
    return t or "Untitled"


def parse_one(raw):
    """Return (info, matched). info = title/season/episode/year."""
    s = clean(raw)
    # TV: S01E10 or 1x10
    m = re.search(
        r"(?i)^(.*?)[\s(\[]*\b(?:s(\d{1,2})\s?e(\d{1,3})|(\d{1,2})x(\d{2,3}))\b", s
    )
    if m:
        season = int(m.group(2) or m.group(4))
        ep = int(m.group(3) or m.group(5))
        return {
            "title": finish_title(m.group(1)),
            "season": season,
            "episode": ep,
            "year": None,
        }, True
    # Anime style: "Show Name - 05"
    m = re.search(r"^(.+?)\s+-\s+(\d{1,3})(?:v\d)?(?=\s|$|\[|\()", s)
    if m and not re.search(r"(?:19|20)\d{2}$", m.group(1).strip()):
        return {
            "title": finish_title(m.group(1)),
            "season": 1,
            "episode": int(m.group(2)),
            "year": None,
        }, True
    # Movie: title + year
    mq = QUALITY.search(s)
    cut = (s[: mq.start()] if mq else s).strip()
    m = re.search(r"^(.+?)[\s(\[]+((?:19|20)\d{2})[\s)\]]*$", cut) or re.search(
        r"^(.+?)[\s(\[]+((?:19|20)\d{2})\b", cut
    )
    if m:
        return {
            "title": finish_title(m.group(1)),
            "season": None,
            "episode": None,
            "year": int(m.group(2)),
        }, True
    return {
        "title": finish_title(cut),
        "season": None,
        "episode": None,
        "year": None,
    }, False


def parse(name, caption=""):
    info, ok = parse_one(name)
    if not ok and caption:
        info2, ok2 = parse_one(caption.split("\n")[0])
        if ok2:
            return info2
    return info


def make_key(info):
    k = re.sub(r"[^a-z0-9]+", "", info["title"].lower()) or "untitled"
    if info["season"] is None and info.get("year"):
        k += str(info["year"])
    return k


def apply_alias(alias, key, title):
    for _ in range(5):
        a = alias.get(key)
        if not a:
            break
        key, title = a
    return key, title


