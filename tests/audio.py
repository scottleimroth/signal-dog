"""Makes a synthetic recording for tests: room noise, steady fan hum, and the sounds Signal Dog should react to.
The code lives in the plugin (demo.py), which also uses it for /signaldog demo."""
import _pkg

_pkg.load()
from signal_dog.demo import RATE, synth  # noqa: E402,F401
