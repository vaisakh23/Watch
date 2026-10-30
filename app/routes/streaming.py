import logging
import mimetypes
import queue
import threading
import time
from urllib.parse import quote

from flask import Blueprint, Response, abort, request

from ..config import run
from ..helpers.auth import authed
from ..helpers.chat import required_chat
from ..helpers.streaming import parse_range
from ..settings import CHUNK, PREFETCH
from ..telegram import _get_msg, _next

streaming_blueprint = Blueprint("streaming", __name__)
logger = logging.getLogger(__name__)


@streaming_blueprint.route("/stream/<int:mid>/<path:fname>", methods=["GET", "HEAD"])
def stream(mid, fname):
    if not authed():
        abort(403)
    chat = required_chat()
    try:
        msg = run(_get_msg(chat, mid))
    except Exception:  # noqa: BLE001
        abort(404)
    if not msg or not msg.file:
        abort(404)

    size = msg.file.size
    if not size:
        abort(415)
    mime = (
        msg.file.mime_type
        or mimetypes.guess_type(fname)[0]
        or "application/octet-stream"
    )

    rng = parse_range(request.headers.get("Range"), size)
    if rng == "bad":
        return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
    start, end = rng if rng else (0, size - 1)
    length = end - start + 1
    aligned = start - (start % CHUNK)
    skip = start - aligned

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": mime,
        "Content-Disposition": f'inline; filename="{quote(fname)}"',
        "Cache-Control": "no-store",
    }
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    status = 206 if rng else 200
    if request.method == "HEAD":
        return Response(status=status, headers=headers)

    def open_iter(m, offset):
        return m.client.iter_download(
            m.media, offset=offset, request_size=CHUNK, file_size=size
        )

    q = queue.Queue(maxsize=PREFETCH)
    stop = threading.Event()
    stream_started = time.monotonic()
    bytes_sent = 0

    def put(item):
        while not stop.is_set():
            try:
                q.put(item, timeout=1)
                return True
            except queue.Full:
                pass
        return False

    def producer():
        it = None
        retried = False
        produced = 0
        try:
            it = open_iter(msg, aligned)
            while not stop.is_set() and produced < skip + length:
                try:
                    chunk = bytes(run(_next(it)))
                except StopAsyncIteration:
                    break
                except Exception:  # noqa: BLE001
                    if retried:
                        raise
                    retried = True
                    fresh = run(_get_msg(chat, mid, fresh=True))
                    if not fresh or not fresh.file:
                        raise RuntimeError(
                            "Telegram message or media is no longer available"
                        )
                    try:
                        run(it.close())
                    except Exception:  # noqa: BLE001
                        pass
                    it = open_iter(fresh, aligned + produced)
                    continue

                chunk = chunk[: skip + length - produced]
                if not chunk:
                    break
                produced += len(chunk)
                if not put(chunk):
                    return
        except Exception as e:  # noqa: BLE001
            logger.exception("Telegram stream failed (chat=%s, message=%s)", chat, mid)
            put(e)
        finally:
            put(None)
            if it is not None:
                try:
                    run(it.close())
                except Exception:  # noqa: BLE001
                    pass

    threading.Thread(target=producer, daemon=True).start()
    logger.info(
        "Telegram stream started (chat=%s, message=%s, range=%s-%s)",
        chat,
        mid,
        start,
        end,
    )

    def gen():
        nonlocal bytes_sent
        remaining, to_skip = length, skip
        outcome = "complete"
        try:
            while remaining > 0:
                chunk = q.get()
                if chunk is None:
                    if remaining:
                        outcome = "truncated"
                    break
                if isinstance(chunk, Exception):
                    outcome = "error"
                    break
                chunk = bytes(chunk)
                if to_skip:
                    chunk, to_skip = chunk[to_skip:], b""
                chunk = chunk[:remaining]
                remaining -= len(chunk)
                bytes_sent += len(chunk)
                if chunk:
                    yield chunk
        except GeneratorExit:
            outcome = "cancelled"
            raise
        finally:
            stop.set()
            duration = max(time.monotonic() - stream_started, 0.001)
            logger.info(
                "Telegram stream %s (chat=%s, message=%s, bytes=%s/%s, duration=%.3fs, avg_mbps=%.3f)",
                outcome,
                chat,
                mid,
                bytes_sent,
                length,
                duration,
                bytes_sent * 8 / duration / 1_000_000,
            )

    return Response(gen(), status=status, headers=headers, direct_passthrough=True)
