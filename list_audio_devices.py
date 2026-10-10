"""
List every audio input device the OS exposes — name, index, channels, rate.
Use this to pick the right value for SESSION_MIC / WAKE_MIC env vars.

Run:
    uv run python list_audio_devices.py
"""

import pyaudio


def main() -> None:
    pa = pyaudio.PyAudio()
    print(f"Default input device index: {pa.get_default_input_device_info()['index']}")
    print(f"Default input name:        {pa.get_default_input_device_info()['name']}")
    print()
    print(f"{'idx':>3}  {'in':>3}  {'rate':>6}  name")
    print("-" * 70)
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        in_ch = int(info["maxInputChannels"])
        if in_ch <= 0:
            continue  # skip output-only
        rate = int(info["defaultSampleRate"])
        print(f"{i:>3}  {in_ch:>3}  {rate:>6}  {info['name']}")
    pa.terminate()
    print()
    print("To use a specific mic, set in your .env file:")
    print('    SESSION_MIC="USB Microphone"      # substring of the name above')
    print('    WAKE_MIC="Microphone Array"        # leave unset to use system default')


if __name__ == "__main__":
    main()
