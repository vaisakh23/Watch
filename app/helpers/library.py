from ..database import dbx
from .presentation import human
from .tmdb import metadata_for


def topic_list(chat):
    names = {r["topic_id"]: r["title"] for r in dbx("SELECT topic_id, title FROM topics WHERE chat=?", (chat,))}
    counts = {r["topic"]: r["c"] for r in dbx(
        "SELECT topic, COUNT(DISTINCT key) AS c FROM items WHERE chat=? GROUP BY topic", (chat,))}
    out = []
    for tid in sorted(set(names) | set(counts)):
        if tid == 1:
            title = names.get(1) or ("General" if names else "All files")
        else:
            title = names.get(tid) or f"Topic {tid}"
        out.append({"id": tid, "title": title, "n": counts.get(tid, 0)})
    return [x for x in out if x["n"]]


def group_rows(chat, where="", args=(), limit=None):
    sql = ("SELECT key, MIN(title) AS title, MAX(date) AS latest, COUNT(*) AS n, "
           "COUNT(DISTINCT season) AS seasons, COUNT(episode) AS eps, MAX(year) AS year, MAX(size) AS size "
           f"FROM items WHERE chat=? {where} GROUP BY key ORDER BY latest DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    rows = dbx(sql, (chat,) + tuple(args))
    metadata = metadata_for(chat, [r["key"] for r in rows])
    for r in rows:
        g = dict(r)
        g["tmdb"] = metadata.get(g["key"])
        if g["tmdb"]:
            g["poster_url"] = ("https://image.tmdb.org/t/p/w500" + g["tmdb"]["poster_path"]
                                if g["tmdb"]["poster_path"] else None)
            g["backdrop_url"] = ("https://image.tmdb.org/t/p/w1280" + g["tmdb"]["backdrop_path"]
                                  if g["tmdb"]["backdrop_path"] else None)
            g["year"] = int((g["tmdb"]["release_date"] or "0000")[:4]) or g["year"]
        if g["eps"]:
            g["sub"] = f'{g["seasons"]} season{"s" if g["seasons"] != 1 else ""} \u00b7 {g["eps"]} ep{"s" if g["eps"] != 1 else ""}'
        elif g["tmdb"]:
            bits = (["★ %.1f" % g["tmdb"]["vote_average"]] if g["tmdb"]["vote_average"] else [])
            if g["year"]:
                bits.append(str(g["year"]))
            g["sub"] = " \u00b7 ".join(bits) or human(g["size"])
        else:
            g["sub"] = " \u00b7 ".join(x for x in (str(g["year"]) if g["year"] else "", human(g["size"])) if x)
        out.append(g)
    return out
