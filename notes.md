1. Needs python3 (3.8+) and ffmpeg (mac: brew install ffmpeg). Nothing else to install.
2. Run: cd topaz-batch && python3 app.py   (try first with: python3 app.py --demo  = fake API, no credits)
3. It opens http://127.0.0.1:8765. Paste your key, pick the input + output folders, scan, set things up, estimate, start.

Notes
- Your key stays on your computer and only ever goes to api.topazlabs.com. "remember" saves it to ~/.topaz_batch/api_key (only you can read it).
- Output = <original name>_upscaled.<mp4|mov|mkv> in your output folder. The box under the board shows the exact request each change produces.
- fps: pick it in the "output fps" box under resolution (keep source, 24/25/30/50/60..., or custom 15-240). The app adds the fps engine (frame interpolation) by itself on clips that need it: Apollo apo-8 by default, or whichever you pick in "fps engine". Clips already at that fps skip it, so no extra cost. Each clip row shows source fps -> output fps.
- Parallel: Topaz has no fixed limit on jobs at once for self-serve accounts. Set "clips at the same time" (default 3, max 10). If Topaz gets busy (429) the app waits and retries.
- "get credit estimate" is free: it creates the requests and shows the cost, but nothing is charged until you press start.
- If you quit mid-batch, the jobs keep running at Topaz. Start the app again and scan the same folders: paid clips show "resumable" and get picked up again, with no second charge.
- Never pays twice (v4): every job Topaz accepted is remembered (in .topaz_batch_state.json in the output folder) and never dropped. A clip that already has a paid job is resumed or downloaded, never sent again. If a paid job failed, got lost, or lives on another key's account, the clip shows "held" with a "re-run (charges again)" button: nothing re-runs without that click.
- Credit check before start (v4): "start upscaling" first shows the credit total for the new clips and only charges after you say ok. Clips already paid resume free.
- "fetch finished (free)" (v4): asks Topaz about every job you already paid for (old runs and v3 runs included) and downloads whatever is finished. Never charges. Jobs on another account need that account's key pasted first.
- Only what's left runs (v3): "scan" checks every clip against the output folder (and its subfolders). A clip counts as done only if a <name>_upscaled file exists (any extension) and is complete: not empty, readable, same length as the source. Empty or cut-off files are shown as "partial" and redone. The scan line says: N done (skipped), M remaining, K partial.
- Out of credits mid-batch: the app stops starting new clips by itself. Clips that hit the limit go back to "remaining". Top up or change the key, scan the same folders again, start.
- Change key: hit "change key", paste the new key, save. The old key is wiped from this computer (also when "remember" is off). Or set TOPAZ_API_KEY in your terminal before running. Never paste the key in any chat. Clips that were still processing on the OLD account can't be seen by the new key: they show "old-key job lost" and re-run on start.
- Stop: no new clips start, and the clips already processing at Topaz show "resumable" (they keep running there, start resumes them without a new charge). You can rescan right away.
- Topaz keeps outputs for 7 days. Uploads over 500 MB are split into parts automatically.
