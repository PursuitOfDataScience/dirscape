"""Running external commands, with a recorded mode so tests can be honest.

Every backend in this package talks to the outside world through a `Runner`
rather than calling `subprocess` itself. That buys three things:

1. **Cross-cluster fixtures.** `RecordedRunner` replays captured output, so the
   GPFS backend can be tested against a real meadow2 transcript from a laptop.
   This is the only reason the portability tests are affordable.
2. **One budget for the whole run.** A tool that promises to finish in under a
   second cannot let one hung mount spend thirty.
3. **One place that knows exit codes lie.** See `Completed.failed`.

The recorded mode carries a hazard worth naming, because `nodetop` shipped it:
NT-1 was a replay that restored a flag meaning "the probe ran" without
restoring the probe's result, so queues that had been *measured* as refusing
came back looking merely unchecked. So `RecordedRunner` raises `NotRecorded`
for a command it has no transcript of, rather than returning an empty success.
An absent recording is a gap in the fixture, never an answer.

Python 3.6 compatible: no `subprocess.run(capture_output=...)`, no f-strings in
hot paths, no dataclasses.
"""

import contextlib
import os
import subprocess
import time
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "Completed",
    "NotRecorded",
    "Runner",
    "SubprocessRunner",
    "CapturingRunner",
    "RecordedRunner",
    "Budget",
    "key_for",
]


def key_for(argv):
    # type: (Sequence[str]) -> str
    """The transcript key for a command.

    Joined with NUL so an argument containing a space cannot collide with two
    arguments, which matters because `mmlsquota --block-size auto` and
    `mmlsquota "--block-size auto"` are different commands.
    """
    return "\x00".join(argv)


class NotRecorded(KeyError):
    """A recorded runner was asked for a command it has no transcript of."""


class Completed(object):
    """The result of one command.

    ``timed_out`` and ``not_found`` are separate fields rather than sentinel
    return codes, because the caller needs to tell "this site has no such
    tool" (durable) from "the tool hung" (transient) from "the tool answered
    and said no" (durable). Collapsing them is how a missing backend gets
    reported as an empty quota.
    """

    __slots__ = ("argv", "returncode", "stdout", "stderr", "elapsed_s", "timed_out", "not_found")

    def __init__(
        self,
        argv,  # type: Sequence[str]
        returncode,  # type: Optional[int]
        stdout="",  # type: str
        stderr="",  # type: str
        elapsed_s=0.0,  # type: float
        timed_out=False,  # type: bool
        not_found=False,  # type: bool
    ):
        # type: (...) -> None
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.elapsed_s = elapsed_s
        self.timed_out = timed_out
        self.not_found = not_found

    @property
    def failed(self):
        # type: () -> bool
        """Whether this command failed, without relying only on the exit status.

        A non-zero code is sufficient evidence of failure. It is not NECESSARY
        evidence of it, which is the part that costs people bugs.

        Two measurements, both taken on a GPFS login node on 2026-09-21, and
        the first one corrects an earlier note in this file that was wrong:

        * GPFS's `mm*` commands DO set a real exit code here. Bare `mmlsquota`
          exits 22, `mmlsquota -u root` exits 1, `mmlsfileset` exits 2, all
          with their text on stderr. An earlier version of this docstring
          claimed they exit 0 on failure; that reading came from a probe piped
          into `head`, where `$?` is head's status and not the command's. Do
          not repeat that measurement technique.

        * The dangerous case is real but different: `mmlsquota -Y <device>`
          against a filesystem the user holds nothing on exits **0**, writes
          **nothing to stderr**, and emits exactly one line, the `:HEADER:`
          field-name row with no records after it. That is a successful command
          with no data, and a backend that treats "it worked" as "here is your
          usage" reports a quota'd filesystem as having no quota.

        So this property catches the cheap cases, and it is deliberately NOT
        the whole story: a backend must also assert positively that it parsed a
        data row, rather than inferring success from the absence of failure.
        Slurm's clients are the reason to keep the stderr rule anyway, since on
        one node for one failure `sacct -u` exits 1 while `squeue -u` exits 0
        and writes only to stderr (slurmwatch SW-90).
        """
        if self.timed_out or self.not_found:
            return True
        if self.returncode is None:
            return True
        if self.returncode != 0:
            return True
        # Exited 0, said nothing, and complained. That is a failure.
        return bool(not self.stdout.strip() and self.stderr.strip())

    @property
    def diagnostic(self):
        # type: () -> str
        """The most informative one-line explanation available.

        Prefers stderr, because the case this exists for is rapiDU's RD-2: an
        exit of 127 was reported as "`quota` is not on PATH" while discarding
        the stderr that named the real cause, which was a site wrapper pointing
        at a directory that no longer existed.
        """
        if self.timed_out:
            return "timed out after %.1fs" % (self.elapsed_s,)
        if self.not_found:
            return "%s not found" % (self.argv[0] if self.argv else "command",)
        text = self.stderr.strip() or self.stdout.strip()
        if text:
            return text.splitlines()[0]
        if self.returncode:
            return "exited %d with no output" % (self.returncode,)
        return "no output"

    def __repr__(self):
        # type: () -> str
        return "Completed(%r, rc=%r, failed=%s)" % (self.argv[:2], self.returncode, self.failed)


class Budget(object):
    """A wall-clock allowance shared across every command in one run.

    Rationed rather than per-command, because the guarantee the tool makes is
    about the whole invocation. `remaining` shrinking to zero turns subsequent
    probes into NOT_PROBED, which is a truthful unknown, instead of letting the
    run overrun its promise.
    """

    __slots__ = ("total_s", "started", "_spent")

    def __init__(self, total_s=8.0):
        # type: (float) -> None
        self.total_s = total_s
        self.started = time.time()
        self._spent = 0.0

    @property
    def remaining(self):
        # type: () -> float
        return max(0.0, self.total_s - (time.time() - self.started))

    @property
    def exhausted(self):
        # type: () -> bool
        return self.remaining <= 0.0

    def slice_for(self, share=0.5, floor=0.35, ceiling=4.0):
        # type: (float, float, float) -> float
        """How long a single command may take, given what is left.

        The floor exists because a slice below it is not worth spending: a GPFS
        wrapper takes about 0.3s to start, so a 0.1s allowance guarantees a
        timeout that teaches nobody anything.
        """
        left = self.remaining
        if left <= 0.0:
            return 0.0
        return max(min(left * share, ceiling), min(floor, left))

    def charge(self, seconds):
        # type: (float) -> None
        self._spent += seconds

    @property
    def spent(self):
        # type: () -> float
        return self._spent


class Runner(object):
    """Interface. Subclasses decide where command output comes from."""

    def run(self, argv, timeout=None, env=None):
        # type: (Sequence[str], Optional[float], Optional[Dict[str, str]]) -> Completed
        raise NotImplementedError

    def available(self, name, extra_dirs=()):
        # type: (str, Sequence[str]) -> Optional[str]
        raise NotImplementedError


def _which(name, extra_dirs=()):
    # type: (str, Sequence[str]) -> Optional[str]
    """Locate an executable on PATH, then in explicitly named directories.

    The `extra_dirs` half is not a convenience. GPFS's tools are **not on
    PATH** on this cluster and are world-executable at `/usr/lpp/mmfs/bin`:

        $ command -v mmlsquota          -> (nothing)
        $ ls -l /usr/lpp/mmfs/bin/mmlsquota
        -r-xr-xr-x 1 root root 24328 mmlsquota

    A tool that probes with `shutil.which` alone concludes GPFS is absent on a
    GPFS cluster.
    """
    if os.path.isabs(name):
        return name if os.access(name, os.X_OK) else None
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    for directory in list(path_dirs) + list(extra_dirs):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class SubprocessRunner(Runner):
    """Runs commands for real."""

    def __init__(self, budget=None):
        # type: (Optional[Budget]) -> None
        self.budget = budget
        self._which_cache = {}  # type: Dict[str, Optional[str]]
        #: Told as each command starts and ends, for the startup board
        #: (`motion.Board`), or None. It sees the command, never its output.
        self.observer = None  # type: Optional[object]

    def available(self, name, extra_dirs=()):
        # type: (str, Sequence[str]) -> Optional[str]
        cache_key = name + "\x00" + "\x00".join(extra_dirs)
        if cache_key not in self._which_cache:
            self._which_cache[cache_key] = _which(name, extra_dirs)
        return self._which_cache[cache_key]

    def run(self, argv, timeout=None, env=None):
        # type: (Sequence[str], Optional[float], Optional[Dict[str, str]]) -> Completed
        watch = self.observer
        if watch is None:
            return self._run(argv, timeout, env)
        # The observer is told, and can never change what it is told about:
        # anything it raises is its own problem, not the command's.
        token = None  # type: object
        try:
            token = watch.started(list(argv))  # type: ignore[attr-defined]
        except Exception:
            watch = None
        result = None  # type: Optional[Completed]
        try:
            result = self._run(argv, timeout, env)
            return result
        finally:
            if watch is not None:
                with contextlib.suppress(Exception):
                    watch.finished(token, result)  # type: ignore[attr-defined]

    def _run(self, argv, timeout=None, env=None):
        # type: (Sequence[str], Optional[float], Optional[Dict[str, str]]) -> Completed
        argv = list(argv)
        if timeout is None and self.budget is not None:
            timeout = self.budget.slice_for()
        if timeout is not None and timeout <= 0.0:
            return Completed(argv, None, elapsed_s=0.0, timed_out=True)

        run_env = dict(os.environ)
        if env:
            run_env.update(env)
        # Site wrappers and GPFS tools are sensitive to locale: a localised
        # decimal separator or a translated error string breaks every parser
        # downstream. Pinning C is cheaper than making each parser tolerant.
        run_env["LC_ALL"] = "C"
        run_env["LANG"] = "C"

        started = time.time()
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=run_env,
                universal_newlines=True,
            )
        except FileNotFoundError:
            return Completed(argv, None, elapsed_s=time.time() - started, not_found=True)
        except OSError as exc:
            return Completed(
                argv, None, stderr=str(exc), elapsed_s=time.time() - started, not_found=True
            )

        try:
            out, err = proc.communicate(timeout=timeout)
            elapsed = time.time() - started
        except subprocess.TimeoutExpired:
            # Kill rather than terminate. A wedged GPFS client ignores SIGTERM
            # while it waits on the network, and the point of the timeout is
            # that the tool returns regardless.
            proc.kill()
            try:
                out, err = proc.communicate(timeout=1.0)
            except Exception:
                out, err = "", ""
            elapsed = time.time() - started
            if self.budget is not None:
                self.budget.charge(elapsed)
            return Completed(argv, None, out or "", err or "", elapsed_s=elapsed, timed_out=True)

        if self.budget is not None:
            self.budget.charge(elapsed)
        return Completed(argv, proc.returncode, out or "", err or "", elapsed_s=elapsed)


class CapturingRunner(Runner):
    """Runs for real and records every transcript, for building fixtures.

    `dirscape snapshot` uses this, which is how a one-command capture on an
    unfamiliar cluster becomes a regression test for a cluster the author has
    no account on.
    """

    def __init__(self, inner=None):
        # type: (Optional[Runner]) -> None
        self.inner = inner or SubprocessRunner()
        self.transcripts = {}  # type: Dict[str, Dict[str, object]]
        self.probes = {}  # type: Dict[str, Optional[str]]

    def available(self, name, extra_dirs=()):
        # type: (str, Sequence[str]) -> Optional[str]
        found = self.inner.available(name, extra_dirs)
        self.probes[name] = found
        return found

    def run(self, argv, timeout=None, env=None):
        # type: (Sequence[str], Optional[float], Optional[Dict[str, str]]) -> Completed
        result = self.inner.run(argv, timeout=timeout, env=env)
        self.transcripts[key_for(argv)] = {
            "argv": list(argv),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "elapsed_s": round(result.elapsed_s, 4),
            "timed_out": result.timed_out,
            "not_found": result.not_found,
        }
        return result

    def to_json(self):
        # type: () -> Dict[str, object]
        return {"probes": dict(self.probes), "commands": list(self.transcripts.values())}


class RecordedRunner(Runner):
    """Replays captured transcripts. Raises on anything it has not seen.

    The raise is the whole point. Returning an empty success for an unrecorded
    command would make a fixture's *gaps* look like a cluster's *answers*,
    which is NT-1: a replay that renders an absent probe as a positive result.
    """

    def __init__(self, transcripts, probes=None, strict=True):
        # type: (Sequence[Dict[str, object]], Optional[Dict[str, Optional[str]]], bool) -> None
        self._by_key = {}  # type: Dict[str, Dict[str, object]]
        for entry in transcripts:
            argv = entry.get("argv") or []
            self._by_key[key_for([str(a) for a in argv])] = entry  # type: ignore[arg-type]
        self._probes = dict(probes or {})
        self.strict = strict
        self.unrecorded = []  # type: List[str]

    @classmethod
    def from_json(cls, payload, strict=True):
        # type: (Dict[str, object], bool) -> "RecordedRunner"
        commands = payload.get("commands") or []
        probes = payload.get("probes") or {}
        return cls(commands, probes, strict=strict)  # type: ignore[arg-type]

    def available(self, name, extra_dirs=()):
        # type: (str, Sequence[str]) -> Optional[str]
        if name in self._probes:
            found = self._probes[name]
            return str(found) if found else None
        if self.strict:
            raise NotRecorded("no recorded availability probe for %r" % (name,))
        self.unrecorded.append(name)
        return None

    def run(self, argv, timeout=None, env=None):
        # type: (Sequence[str], Optional[float], Optional[Dict[str, str]]) -> Completed
        key = key_for(argv)
        entry = self._by_key.get(key)
        if entry is None:
            if self.strict:
                raise NotRecorded("no recorded output for: %s" % (" ".join(argv),))
            self.unrecorded.append(" ".join(argv))
            # Non-strict mode still refuses to fabricate a success: an
            # unrecorded command comes back as a transient failure so callers
            # render it unknown.
            return Completed(argv, None, stderr="not recorded", timed_out=True)
        return Completed(
            argv,
            entry.get("returncode"),  # type: ignore[arg-type]
            str(entry.get("stdout") or ""),
            str(entry.get("stderr") or ""),
            elapsed_s=float(entry.get("elapsed_s") or 0.0),
            timed_out=bool(entry.get("timed_out")),
            not_found=bool(entry.get("not_found")),
        )
