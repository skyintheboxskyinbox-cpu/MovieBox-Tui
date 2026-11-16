# Oracle setup — MovieBox bridge (Always Free VM)

Host the rewritten MovieBox bridge on an Oracle Cloud Always Free VM.
This is the short-term test host and the long-term 24x7 host. The code
lives on the fork branch `moviebox-watch-partial` — nothing here depends
on a machine that can disappear.

## VM you need

- Shape: VM.Standard.A1.Flex, 1 OCPU / 4 GB RAM is enough for MovieBox
  (the free pool is 2 OCPU / 12 GB — the rest can be a second VM later).
- Image: Ubuntu 24.04 LTS.
- Networking: a public IP (Oracle gives one; reserve it so it never
  changes). Open ingress TCP **8000** (Watch page) and **7681** (the
  terminal / App) in the subnet security list, source 0.0.0.0/0.
- No Cloudflare tunnel and no domain are needed — the VM has a direct
  public IP. HTTP is fine for playback.

## Install (on the VM, over SSH)

```bash
sudo apt update && sudo apt install -y python3 tmux ffmpeg curl
# ttyd (serves the terminal on port 7681)
sudo apt install -y ttyd || { curl -L -o /tmp/ttyd \
  https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 \
  && chmod +x /tmp/ttyd && sudo mv /tmp/ttyd /usr/local/bin/ttyd; }
# yt-dlp
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
  -o /usr/local/bin/yt-dlp && sudo chmod +x /usr/local/bin/yt-dlp
# MovieBox-TUI v0.1.29 (the app itself)
curl -L -o /tmp/moviebox-tui \
  https://github.com/mesamirh/MovieBox-Tui/releases/download/v0.1.29/moviebox-tui-linux-x86_64 \
  && chmod +x /tmp/moviebox-tui && sudo mv /tmp/moviebox-tui /usr/local/bin/moviebox-tui
```

## Get the code

```bash
git clone -b moviebox-watch-partial \
  https://github.com/skyintheboxskyinbox-cpu/MovieBox-Tui.git /tmp/mbt
cp -r /tmp/mbt/moviebox-watch ~/moviebox-watch
```

## Run it

```bash
cd ~/moviebox-watch
SKIP_TUNNELS=1 bash start.sh
```

`SKIP_TUNNELS=1` is the Oracle mode: no Cloudflare tunnels are started,
because the VM already has a direct public IP. `start.sh` starts the
app (tmux session `moviebox`), ttyd on 7681, the Python server on 8000,
the sync loop, the watchdog and keepalive — all of it, idempotent, safe
to re-run.

Then open the page at `http://<your-public-IP>:8000/` on the phone.
The terminal iframe is served from `http://<your-public-IP>:7681/` —
if it ever shows blank, open that address directly to check ttyd.

## Notes

- Downloads stay on the VM (`~/Downloads/MovieBox-TUI/`). The Watch
  page is the player; search stays in the terminal.
- The watchdog stops the setup after 15 minutes idle (heartbeat-gated,
  playback-aware). To stop the VM yourself: hold the Online pill 4 s
  on the page → Shut down.
- Keep the VM busy (watch / download at least once a week): Oracle can
  reclaim a VM that is idle for 7 days.
- If the page ever goes down, SSH in and re-run
  `SKIP_TUNNELS=1 bash ~/moviebox-watch/start.sh` — it restores
  everything and prints the fresh URL.
- Never keep code only on the VM. Any fix goes back to the fork branch
  `moviebox-watch-partial` first (see REBUILD-SPEC.md for the bridge
  behaviour).
