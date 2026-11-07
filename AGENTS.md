# Signal Dog: project rules

Rules for anyone, person or agent, working in this repository.

## Code

- Standard Python only: no packages to install. `ffmpeg` must already be installed; the plugin never downloads it.
- `detect.py` is numbers in, events out: no audio, no ffmpeg, no file or network access.
- Tests: `python3 -m unittest discover -s tests` (the demo and recording tests need `ffmpeg`).

## Privacy

- No personal names beyond the author's, and no addresses, locations, household details
  or machine details in code, logs, file names or commit messages. Use example values.
- No secrets, tokens or keys anywhere.
- The plugin never writes microphone audio to disk. Sound is analysed in memory and dropped. The one sound file it
  writes is the demo's, made by code (no real sound) in a private temporary folder and deleted when the demo ends.
- The plugin itself makes no network calls. Its only output is a chat message through
  the agent it is installed in.

## Commits

- No AI co-author lines.
- No force pushes.
