# TG UI

A small Telegram media library and streaming interface for Termux and desktop Linux. It uses Telethon for Telegram access and Flask for the local web UI.

## Run

1. Create a Telegram API application at [my.telegram.org](https://my.telegram.org) and set `TG_API_ID` and `TG_API_HASH` in `.env` or the environment.
2. For movie posters and metadata, create a TMDB API Read Access Token in your TMDB account settings and set `TMDB_ACCESS_TOKEN` in `.env`. `TMDB_API_KEY` (v3 key) also works. Metadata is matched and cached locally as the library loads; set `TMDB_LANGUAGE` to change the default `en-US` search language.
3. Install dependencies with `uv sync`.
4. Start the app with `uv run tg-ui` (or `uv run python -m tg_ui`).
5. Open `http://localhost:5000` and sign in to Telegram.

The app stores its session and media index in your home directory: `~/tg_ui_session`, `~/tg_library.db`, and `~/.tg_thumbs`. Downloads go to `~/storage/downloads/Telegram` by default; set `TG_UI_HOST` to change the bind address.
