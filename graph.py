"""The ffmpeg side of Signal Dog: one process turns microphone sound into 64 numbers, 100 times a second.

ffmpeg does all the audio work. It splits the sound into narrow bands with band-pass filters, squares each
band (energy), resamples that to 100 values a second and joins the 64 bands into one stream of 32-bit
floats on a pipe. Python only ever sees those numbers, never the sound.

Channels 0-8   knock bands, 70 to 760 Hz
Channel  9     high band, 1.6 to 6.4 kHz (separates a knock from sharp clicks)
Channels 10-63 beep bands, 64 Hz apart, 1000 to 4448 Hz
"""

from __future__ import annotations

import os
import platform
import re
import subprocess

FRAME_RATE = 100
LOW_BANDS = (70, 95, 128, 172, 232, 312, 420, 565, 760)
TONE_BANDS = tuple(1000 + 64 * k for k in range(54))
N_LOW = len(LOW_BANDS)
IDX_HIGH = N_LOW
IDX_TONE = N_LOW + 1
N_TONE = len(TONE_BANDS)
CHANNELS = N_LOW + 1 + N_TONE          # 64, the most one ffmpeg process can join
FRAME_BYTES = CHANNELS * 4

_TAIL = "aeval=val(0)*val(0):c=same,aresample=%d,aformat=sample_fmts=flt:channel_layouts=mono" % FRAME_RATE


def _square(i: int) -> str:
    """Square the band (its energy) by multiplying it with itself, then bring it down to 100 values a second.
    A plain multiply is far cheaper than an expression evaluated for every sample."""
    return "asplit=2[q%d][r%d];[q%d][r%d]amultiply,aresample=%d,aformat=sample_fmts=flt:channel_layouts=mono[e%d]" % (
        i, i, i, i, FRAME_RATE, i)


def _band(i: int, hz: int, width: int) -> str:
    # two band-pass stages in a row make the skirts steeper
    return "[s%d]bandpass=f=%d:width_type=h:w=%d,bandpass=f=%d:width_type=h:w=%d,%s" % (i, hz, width, hz, width, _square(i))


def filter_graph() -> str:
    # a microphone arrives at 48 kHz stereo; mono first, then 16 kHz, so the 64 filters do a third of the work
    # and in 100 ms chunks: a live microphone hands over tiny slices, and every slice walks through all 64 branches
    parts = ["[0:a]aformat=channel_layouts=mono,aresample=16000,asetnsamples=n=1600:p=0,asplit=%d%s" % (
        CHANNELS, "".join("[s%d]" % i for i in range(CHANNELS)))]
    for i, hz in enumerate(LOW_BANDS):
        parts.append(_band(i, hz, max(20, hz // 3)))
    parts.append("[s%d]highpass=f=1600,highpass=f=1600,lowpass=f=6400,%s" % (IDX_HIGH, _square(IDX_HIGH)))
    for k, hz in enumerate(TONE_BANDS):
        parts.append(_band(IDX_TONE + k, hz, 64))
    parts.append("%sjoin=inputs=%d:channel_layout=%dC[out]" % ("".join("[e%d]" % i for i in range(CHANNELS)), CHANNELS, CHANNELS))
    return ";".join(parts)


def microphone_input(source: str = "default", system: str = "") -> list:
    """ffmpeg's input for a live microphone. Linux and WSL: PulseAudio (`source` is a PulseAudio source name). Windows:
    DirectShow (`source` is the device name ffmpeg lists; "default" is the first microphone it lists). macOS:
    AVFoundation (`source` is the audio device index or name; "default" is the first, index 0)."""
    system = system or platform.system()
    source = source or "default"
    if system == "Windows":
        name = default_windows_microphone() if source == "default" else source
        return ["-f", "dshow", "-audio_buffer_size", "50", "-i", "audio=" + (name or "")]
    if system == "Darwin":
        return ["-f", "avfoundation", "-i", ":" + ("0" if source == "default" else source)]
    return ["-fflags", "nobuffer", "-f", "pulse", "-i", source]


_DSHOW_AUDIO = re.compile(r'"([^"]+)" \(audio\)')


def default_windows_microphone():
    """The first audio capture device DirectShow lists, or None. ffmpeg prints the list on stderr."""
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                           capture_output=True, timeout=20, env=environment())
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _DSHOW_AUDIO.search(p.stderr.decode("utf-8", "replace"))
    return m.group(1) if m else None


def command(source: str = "default", realtime: bool = True) -> list:
    """The ffmpeg command line. `source` is a microphone (see `microphone_input`), or `file:<path>` to replay a
    recording (in real time unless `realtime` is false)."""
    if source.startswith("file:"):
        inp = ["-re", "-i", source[5:]] if realtime else ["-i", source[5:]]
    else:
        inp = microphone_input(source)
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"] + inp + [
        "-filter_complex", filter_graph(), "-map", "[out]", "-f", "f32le", "-flush_packets", "1", "pipe:1"]


# The only variables ffmpeg is given: what it needs to run, find the sound server and open the microphone.
# Nothing else from the agent's environment (provider keys, tokens) reaches the child process.
ENV_KEEP = (
    # every system
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "LD_LIBRARY_PATH",
    # PulseAudio / PipeWire on Linux and WSL (server address, cookie under the config dir, X11 property, D-Bus)
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
    # Windows (DirectShow)
    "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "COMSPEC",
    "PATHEXT",
)
ENV_KEEP_PREFIXES = ("PULSE_",)


def environment(source=None) -> dict:
    """The environment for ffmpeg: only the names in ENV_KEEP and PULSE_*, matched without regard to case (Windows
    reports SystemRoot as SYSTEMROOT). Under WSL the sound server is a socket PulseAudio clients do not find alone.
    `source` defaults to os.environ; tests pass their own mapping."""
    source = os.environ if source is None else source
    env = {key: value for key, value in source.items()
           if key.upper() in ENV_KEEP or key.upper().startswith(ENV_KEEP_PREFIXES)}
    sock = "/mnt/wslg/PulseServer"
    if "PULSE_SERVER" not in env and os.path.exists(sock):
        env["PULSE_SERVER"] = "unix:" + sock
    return env
