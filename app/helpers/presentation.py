from flask import render_template, request

from ..settings import PORT


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024


def page(**kw):
    kw.setdefault("err", request.args.get("err"))
    kw.setdefault("chat", "")
    kw.setdefault("port", PORT)
    template = {
        "login": "login.html",
        "group_error": "group_error.html",
        "lib": "library.html",
        "show": "show.html",
    }.get(kw.pop("view"))
    if template is None:
        raise ValueError("Unknown page view")
    return render_template(template, **kw)
