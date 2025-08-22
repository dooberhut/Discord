# Dooberhut Bot — Railway Ready

This folder is ready to deploy on **Railway.app**.

## One-time
1. Create an account: https://railway.app/
2. New Project → "Deploy from Repository" (or "Upload" if using the ZIP)
3. Add Environment Variables in the project settings:
   - `DISCORD_TOKEN` = your bot token
   - (optional) `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`

## Deploy
- If uploading the ZIP: drag it into Railway → it builds using the Dockerfile → starts automatically.
- If using GitHub: push these files to a repo, then connect Railway → Deploy.

## Notes
- FFmpeg is included in the Docker image for audio playback.
- The bot keeps a persistent connection to Discord and must run continuously; Railway handles this.
