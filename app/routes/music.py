from flask import Blueprint, abort, jsonify, redirect, request

from ..helpers.auth import authed, err_redirect
from ..helpers.chat import required_chat
from ..helpers.presentation import page
from ..music import jobs, jobs_lock, playlist_rows, start_import
from urllib.parse import quote

music_blueprint = Blueprint("music", __name__)


@music_blueprint.route("/music")
def music():
    if not authed():
        return redirect("/")
    chat = required_chat()
    with jobs_lock:
        job = dict(jobs.get(chat, {}))
    playlists = playlist_rows(chat)
    for playlist in playlists:
        for track in playlist["tracks"]:
            if track["message_id"]:
                track["stream_url"] = f"/stream/{track['message_id']}/{quote(track['fname'] or 'audio.mp3')}"
    return page(view="music", playlists=playlists, job=job)


@music_blueprint.route("/music/import", methods=["POST"])
def music_import():
    if not authed():
        abort(403)
    chat = required_chat()
    try:
        start_import(chat, request.form.get("url", ""))
    except Exception as exc:
        return err_redirect("/music", exc)
    return redirect("/music")


@music_blueprint.route("/music/status")
def music_status():
    if not authed():
        abort(403)
    chat = required_chat()
    with jobs_lock:
        return jsonify(jobs.get(chat, {}))


@music_blueprint.route("/music/playlist/<path:playlist_id>")
def music_playlist(playlist_id):
    if not authed():
        return redirect("/")
    chat = required_chat()
    playlists = [p for p in playlist_rows(chat) if p["playlist_id"] == playlist_id]
    if not playlists:
        return redirect("/music")
    playlist = playlists[0]
    for track in playlist["tracks"]:
        if track["message_id"]:
            track["stream_url"] = f"/stream/{track['message_id']}/{quote(track['fname'] or 'audio.mp3')}"
    return page(view="music_playlist", playlist=playlist)
