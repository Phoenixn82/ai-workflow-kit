# motion beats <track> -> beats.json on stdout: bpm, beats (state changes), downbeats (big moments), hits (SFX).
# Produced music sits on a fixed grid, so tracker jitter is fitted away: if a constant tempo explains the
# beats within 35 ms, beats become that steady grid across the whole track. Live/rubato tracks stay raw.
import sys, json, numpy as np, librosa

y, sr = librosa.load(sys.argv[1], sr=None, mono=True)
dur = len(y) / sr
tempo, frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
raw = librosa.frames_to_time(frames, sr=sr)
onset = librosa.onset.onset_strength(y=y, sr=sr)
peaks = librosa.util.peak_pick(onset, pre_max=3, post_max=3, pre_avg=3, post_avg=5, delta=0.5, wait=10)

beats, steady = raw, False
if len(raw) >= 8:
    idx = np.arange(len(raw))
    keep = np.ones(len(raw), bool)
    for _ in range(3):  # least-squares grid with outlier rejection
        period, t0 = np.polyfit(idx[keep], raw[keep], 1)
        resid = raw - (t0 + period * idx)
        keep = np.abs(resid) < 0.06
    if keep.sum() >= 0.8 * len(raw) and np.std(resid[keep]) < 0.035:
        while t0 - period > -0.03: t0 -= period  # extend the grid to the start (a beat a few ms before 0 counts)...
        beats, steady = np.arange(t0, dur, period), True  # ...and to the end of the track
        tempo = 60 / period

json.dump({
    "bpm": round(float(np.atleast_1d(tempo)[0]), 2),
    "steady": steady,
    "beats": np.round(beats, 3).tolist(),
    "downbeats": np.round(beats[::4], 3).tolist(),  # ponytail: assumes 4/4 with beat 1 first; cut tracks on a downbeat
    "hits": librosa.frames_to_time(peaks, sr=sr).round(3).tolist(),
}, sys.stdout, indent=1)
