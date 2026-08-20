"""A one-way progress channel from spatialize_cli.py to the server.

The CLI prints ordinary human lines to stdout and, interleaved with them,
machine-readable lines prefixed with MARK. The server streams stdout line by
line, folds the marked ones into a job record, and keeps the rest as a log
tail. Nothing here imports the DSP, so both sides can be tested on their own.
"""
import json
import time

MARK = "@@PROG "

# Display order. Weights are supplied per-run by the CLI, because a cache hit
# skips separation entirely and would otherwise leave the bar stuck at 8%.
LABELS = {
    "queued":    "Queued",
    "decode":    "Decoding source",
    "separate":  "Separating stems",
    "envelopes": "Measuring stem levels",
    "mix":       "Building the mix",
    "convolve":  "HRTF convolution",
    "encode":    "Encoding output",
}


def emit(stage, **fields):
    """Called by the CLI. Unbuffered so the server sees it as it happens."""
    print(MARK + json.dumps({"stage": stage, **fields}), flush=True)


def parse(line):
    """Return the payload of a marked line, or None for ordinary output."""
    if not line.startswith(MARK):
        return None
    try:
        payload = json.loads(line[len(MARK):])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


class Tracker:
    """Server-side accumulator: turns a stream of lines into a job snapshot."""

    LOG_LINES = 40

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.started = clock()
        self.plan = []
        self.stage = "queued"
        self.detail = {}
        self.log = []
        self.error = None
        self.result = None
        self._done_weight = 0.0
        self._total_weight = 0.0
        self._frac = 0.0
        self._floor = 0.0

    # -- ingest ------------------------------------------------------------
    def feed(self, line):
        line = line.rstrip("\n")
        if not line:
            return
        payload = parse(line)
        if payload is None:
            self.log.append(line)
            del self.log[:-self.LOG_LINES]
            return
        self._apply(payload)

    def _apply(self, p):
        stage = p.get("stage")
        if stage == "plan":
            # The plan is re-issued once the cache state is known, which can
            # drop a stage entirely. Re-credit finished weight against the new
            # plan, or the bar jumps backwards the moment it is revised.
            self.plan = p.get("stages", [])
            self._total_weight = sum(s.get("weight", 1) for s in self.plan) or 1.0
            ids = [s["id"] for s in self.plan]
            if self.stage in ids:
                self._done_weight = sum(s.get("weight", 1)
                                        for s in self.plan[:ids.index(self.stage)])
            return
        if stage == "result":
            self.result = p.get("meta", {})
            self.stage = "done"
            self._frac = 1.0
            self._done_weight = self._total_weight
            return
        if stage == "error":
            self.error = p.get("message", "")
            self.stage = "error"
            return

        if stage != self.stage:
            # Everything before the new stage counts as finished, so a skipped
            # stage (cached stems) advances the bar instead of stranding it.
            if stage in [s["id"] for s in self.plan]:
                upto = [s["id"] for s in self.plan].index(stage)
                self._done_weight = sum(s.get("weight", 1) for s in self.plan[:upto])
            self.stage = stage
            self._frac = 0.0
        self._frac = max(self._frac, float(p.get("frac", self._frac) or 0.0))
        detail = {k: v for k, v in p.items() if k not in ("stage", "frac")}
        self.detail = detail
        if p.get("note"):
            self.log.append(p["note"])
            del self.log[:-self.LOG_LINES]

    # -- read out ----------------------------------------------------------
    @property
    def weight(self):
        for s in self.plan:
            if s["id"] == self.stage:
                return s.get("weight", 1)
        return 0.0

    @property
    def pct(self):
        """Monotonic by construction. A revised plan can re-value work that is
        already finished; a ring that runs backwards reads as a bug, so the
        reported figure only ever climbs."""
        if not self._total_weight:
            return 0.0
        p = (self._done_weight + self._frac * self.weight) / self._total_weight
        self._floor = max(self._floor, max(0.0, min(1.0, p)))
        return self._floor

    def eta(self):
        """Seconds remaining, extrapolated from progress so far. None until
        there is enough signal to be worth showing."""
        p = self.pct
        elapsed = self._clock() - self.started
        if p <= 0.02 or elapsed < 2.0 or p >= 1.0:
            return None
        return max(0.0, elapsed * (1.0 - p) / p)

    def snapshot(self):
        return {
            "stage": self.stage,
            "label": LABELS.get(self.stage, self.stage.title()),
            "pct": round(self.pct, 4),
            "frac": round(self._frac, 4),
            "detail": self.detail,
            "plan": self.plan,
            "eta": self.eta(),
            "elapsed": round(self._clock() - self.started, 1),
            "log": list(self.log),
        }
