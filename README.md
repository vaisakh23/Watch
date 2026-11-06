# TG UI

A small Telegram media library and streaming interface for Termux and desktop Linux. It uses Telethon for Telegram access and Flask for the local web UI.

## Run

1. Create a Telegram API application at [my.telegram.org](https://my.telegram.org) and set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in `.env` or the environment.
2. Set `TG_GROUP` to the one group to index, using its `@username` or numeric Telegram ID. Sign in with an account that belongs to the group and can read its history.
3. For movie and TV posters and metadata, create a TMDB API Read Access Token in your TMDB account settings and set `TMDB_ACCESS_TOKEN` in `.env`. `TMDB_API_KEY` (v3 key) also works. Metadata is matched and cached locally as the library loads; set `TMDB_LANGUAGE` to change the default `en-US` search language.
4. Install a supported JavaScript runtime such as Deno on the host for YouTube extraction.
5. Install dependencies with `uv sync`.
6. Start the app with `uv run tg-ui` (or `uv run python -m app`).
7. Open `http://localhost:5000` and sign in to Telegram. The configured group's library opens after login.

The app stores its session and media index in your home directory: `~/tg_ui_session` and `~/tg_library.db`. It binds to `127.0.0.1` by default; set `TG_UI_HOST` if you need a different bind address. Music imports accept YouTube Music playlist links and use yt-dlp to read the playlist and download its audio; no YouTube or Spotify API keys are required. yt-dlp also needs a supported JavaScript runtime such as Deno for current YouTube extraction. The importer keeps the best audio-only source stream available and preserves its original codec rather than converting it to MP3. Audio files are uploaded without descriptive captions to a `Music` forum topic in `TG_GROUP`; playlist and track metadata stays in SQLite. Imports process at most 200 tracks; the account must be able to create a forum topic and upload files. If the group cannot be resolved after login, check that the username or ID is correct and that the account is a member.
