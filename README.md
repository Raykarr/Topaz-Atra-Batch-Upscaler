# Topaz Batch Upscaler

A lightweight local web app for batch upscaling video clips with the Topaz Video API. Choose input and output folders, configure models and frame rates, review a free credit estimate, and track each clip through upload, processing, and download.

> An independent client for the Topaz Video API. This project is not affiliated with or endorsed by Topaz Labs. Live API usage may consume credits.

## Features

- Batch process videos from a selected input folder.
- Configure upscale model, output resolution, frame rate, interpolation, and concurrency (1–10 clips).
- Review the request and get a credit estimate before starting. Credits are charged only when live processing starts.
- Track per-clip progress and resume jobs accepted by Topaz.
- Skip completed outputs on rescans and detect partial outputs.
- Try the interface in demo mode with a fake API and no key or credits.
- Runs locally in your browser.

## Requirements

- Python 3.8 or newer
- FFmpeg, including `ffprobe`, available on your `PATH`
- A Topaz Video API key for live processing (not needed in demo mode)

On macOS, install FFmpeg with Homebrew:

```sh
brew install ffmpeg
```

## Getting started

Download or clone this project and open a terminal in its folder.

Start the demo first:

```sh
python3 app.py --demo
```

Then open [http://127.0.0.1:8765](http://127.0.0.1:8765). Demo mode uses a mock API and does not use credits.

For live processing, stop the demo with **Ctrl+C**, then run:

```sh
python3 app.py
```

Open the same address, enter your Topaz API key, choose input and output folders, scan your clips, review settings and the estimate, and start processing when ready. **Live jobs can consume Topaz credits.**

If port 8765 is already in use, stop the other process using it or check `python3 app.py --help` for the available port option.

## Output and resuming jobs

Output files use the name `<clip-name>_upscaled` and the selected video container, and are saved to the chosen output folder. Job state is stored in `.topaz_batch_state.json` inside that folder. Keep this file to support recovery and avoid accidentally submitting paid jobs again.

Topaz jobs may continue after you close the app. Reopen it and scan the same folders to resume or fetch completed jobs. Topaz output retention is limited; consult Topaz's current API documentation for its retention period.

## API key and privacy

The app runs its web interface on `127.0.0.1` (localhost). Your API key is sent to the Topaz API to make requests. If you select the remember-key option, the key is stored locally under `~/.topaz_batch/`. Do not share your API key or commit it to source control.

## License

No license is included yet. Add a license before accepting contributions or granting permission to reuse this project.
