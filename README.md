# TG UI

A small Telegram media library and streaming interface for Termux and desktop Linux. It uses Telethon for Telegram access and Flask for the local web UI.

## Run

1. Create a Telegram API application at [my.telegram.org](https://my.telegram.org) and set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in `.env` or the environment.
2. Set `TG_GROUP` to the one group to index, using its `@username` or numeric Telegram ID. Sign in with an account that belongs to the group and can read its history.
3. For movie posters and metadata, create a TMDB API Read Access Token in your TMDB account settings and set `TMDB_ACCESS_TOKEN` in `.env`. `TMDB_API_KEY` (v3 key) also works. Metadata is matched and cached locally as the library loads; set `TMDB_LANGUAGE` to change the default `en-US` search language.
4. Install dependencies with `uv sync`.
5. Start the app with `uv run tg-ui` (or `uv run python -m tg_ui`).
6. Open `http://localhost:5000` and sign in to Telegram. The configured group's library opens after login.

The app stores its session and media index in your home directory: `~/tg_ui_session`, `~/tg_library.db`, and `~/.tg_thumbs`. It binds to `127.0.0.1` by default; set `TG_UI_HOST` if you need a different bind address. The app only browses and streams files from `TG_GROUP`; chat browsing, messaging, and downloads are not available. If the group cannot be resolved after login, check that the username or ID is correct and that the account is a member.
