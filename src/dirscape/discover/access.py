"""What you can actually do with a path, measured rather than derived.

**Access is decided with `os.access()` and nothing else. Mode bits are never
parsed and ACLs are never read.** That is a measurement, not a style choice:
across 668 directories under one ``/project``, ``os.access`` agreed with a real
``listdir()`` in 100% of cases, including the 18 that carry POSIX ACLs and the
one carrying the GPFS-native ``mmgetacl`` control bit. A mode-bit parser gets
every one of those 19 wrong, in both directions, because the bits it reads are
not the bits the kernel checks. ``os.access`` asks the kernel the same question
the kernel will answer later.

Three states, not two. ``TRAVERSE`` is real and common: ``/project/marsh`` is
mode 2771, so a non-member gets ``--x`` and can pass through a path they already
know while ``listdir`` raises EACCES. Measured here: ``os.access`` reports
R=False, X=True and ``listdir`` really does refuse. Collapsing that into
"denied" would hide storage the user can reach.

**A probe that does not finish returns UNKNOWN, never CLOSED.** A ``stat`` on a
wedged network mount blocks indefinitely and no timeout on the syscall exists,
so the deadline is enforced by running the call on a daemon thread and
abandoning it. Reporting "closed" about a mount that merely hung is the
inverted form of nodetop's NT-1, and it is the one failure this module exists to
prevent.
"""

import contextlib
import errno
import os
import threading
import time
from collections import namedtuple
from typing import Callable, Optional, Tuple

from ..model import Reach, Verdict, VerdictCategory, confirmed, refuted, unknown

__all__ = [
    "ReachResult",
    "SymlinkInfo",
    "DEFAULT_DEADLINE_S",
    "with_deadline",
    "probe_reach",
    "probe_present",
    "probe_writable",
    "symlink_info",
]


# Two fields exactly, matching ``Root.reach`` and ``Root.reach_reason``. There
# is no third slot on `Root` for a duration, so returning one would invite a
# caller to invent a place to put it.
ReachResult = namedtuple("ReachResult", "state reason")

SymlinkInfo = namedtuple(
    "SymlinkInfo",
    "path target resolved is_dir crosses_device link_dev target_dev",
)


# One second. A warm ``lstat`` on the fifteen GPFS mountpoints here measures
# under 0.02 ms, so a healthy filesystem is three to four orders of magnitude
# inside this and the deadline never fires. It is sized for the case that cannot
# be reproduced on demand, a mount whose metadata server has stopped answering,
# where the only requirement is that one such mount costs one second of an
# eight second budget instead of the whole run.
DEFAULT_DEADLINE_S = 1.0


def with_deadline(func, deadline_s):
    # type: (Callable[[], object], Optional[float]) -> Tuple[bool, object, Optional[BaseException], float]
    """Run ``func`` and give up on it after ``deadline_s``.

    Returns ``(finished, value, exception, elapsed_s)``.

    The thread is a **daemon** thread and that is load-bearing. A ``stat`` on a
    wedged GPFS mount is uninterruptible, so the thread cannot be killed; a
    non-daemon thread would therefore block interpreter shutdown forever, which
    is a worse hang than the one the deadline exists to bound. As a daemon it is
    abandoned and the process still exits.

    Measured cost of the thread itself: median 51 microseconds over 400 calls
    (min 46, p90 60) on this node, so about thirty roots times three probes is
    under 5 ms of overhead. Cheap enough that there is no fast path, and a fast
    path is exactly where a probe that forgets its deadline would hide.
    """
    if deadline_s is not None and deadline_s <= 0:
        return (False, None, None, 0.0)

    box = {}  # type: dict

    def target():
        # type: () -> None
        try:
            box["value"] = func()
        except BaseException as exc:
            # Caught broadly and handed back verbatim rather than handled: this
            # thread has no idea what the caller was asking, and swallowing the
            # exception here would turn a real error into a silent timeout.
            box["exc"] = exc

    started = time.time()
    thread = threading.Thread(target=target)
    thread.daemon = True
    thread.start()
    thread.join(deadline_s)
    elapsed = time.time() - started

    if thread.is_alive():
        return (False, None, None, elapsed)
    if "exc" in box:
        return (True, None, box["exc"], elapsed)
    return (True, box.get("value"), None, elapsed)


def _timeout_reason(deadline_s):
    # type: (Optional[float]) -> str
    return "probe did not finish within %.2fs" % (deadline_s or 0.0,)


def probe_reach(path, deadline_s=DEFAULT_DEADLINE_S):
    # type: (str, Optional[float]) -> ReachResult
    """How far into ``path`` the caller can get.

    ``LISTABLE`` needs R_OK and X_OK together, ``TRAVERSE`` is X_OK without
    R_OK, ``CLOSED`` is neither. Anything that prevents the question from being
    answered, including a missing path, returns ``UNKNOWN``: calling a path that
    is not there "closed" would be a durable claim about access made from no
    evidence about access at all.
    """

    def probe():
        # type: () -> Tuple[bool, bool, bool]
        # All three in one guarded call. Three separate deadlines would let a
        # mount hang twice and would charge the budget three times.
        return (
            os.access(path, os.F_OK),
            os.access(path, os.R_OK),
            os.access(path, os.X_OK),
        )

    finished, value, exc, _elapsed = with_deadline(probe, deadline_s)

    if not finished:
        return ReachResult(Reach.UNKNOWN, _timeout_reason(deadline_s))
    if exc is not None:
        return ReachResult(Reach.UNKNOWN, "access probe failed: %s" % (exc,))

    exists, readable, executable = value  # type: ignore[misc]
    if not exists:
        return ReachResult(Reach.UNKNOWN, "path is not present, so access is undetermined")
    if readable and executable:
        return ReachResult(Reach.LISTABLE, "os.access reports read and execute")
    if executable:
        # The /project/marsh case: mode 2771, so a non-member can cd through it
        # and cannot list it.
        return ReachResult(Reach.TRAVERSE, "os.access reports execute without read")
    if readable:
        # Readable without execute. Rare, and not listable: opendir needs both,
        # so this is the same practical state as closed but a different reason.
        return ReachResult(Reach.CLOSED, "os.access reports read without execute")
    return ReachResult(Reach.CLOSED, "os.access reports neither read nor execute")


def probe_present(path, deadline_s=DEFAULT_DEADLINE_S):
    # type: (str, Optional[float]) -> Verdict
    """Whether ``path`` exists, with the deadline enforced.

    ENOENT is a durable refusal. A timeout is not: it comes back as
    ``PROBE_TIMEOUT`` so the row reads "could not determine" rather than
    claiming a filesystem is gone because one metadata server was busy.
    """

    def probe():
        # type: () -> object
        # lstat, not stat: a dangling symlink is present as a symlink, and
        # following it would let a link into a wedged mount hang a probe about
        # the link itself.
        return os.lstat(path)

    finished, _value, exc, elapsed = with_deadline(probe, deadline_s)

    if not finished:
        return unknown(
            VerdictCategory.PROBE_TIMEOUT,
            _timeout_reason(deadline_s),
            source="os.lstat",
            elapsed_s=elapsed,
        )
    if exc is None:
        return confirmed(source="os.lstat", elapsed_s=elapsed)

    if isinstance(exc, OSError):
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return refuted(
                VerdictCategory.NOT_PRESENT,
                "os.lstat: %s" % (exc.strerror or exc,),
                source="os.lstat",
                elapsed_s=elapsed,
            )
        if exc.errno in (errno.EACCES, errno.EPERM):
            # Denied on a parent directory. The path may well exist, and saying
            # it does not would be a refusal drawn from an unanswered question.
            return unknown(
                VerdictCategory.UNKNOWN,
                "cannot stat: %s" % (exc.strerror or exc,),
                source="os.lstat",
                elapsed_s=elapsed,
            )
        if exc.errno in (errno.ESTALE, errno.EIO, errno.EHOSTDOWN, errno.ENOTCONN):
            return unknown(
                VerdictCategory.BACKEND_FAILED,
                "filesystem error: %s" % (exc.strerror or exc,),
                source="os.lstat",
                elapsed_s=elapsed,
            )
    return unknown(
        VerdictCategory.BACKEND_FAILED,
        "stat failed: %s" % (exc,),
        source="os.lstat",
        elapsed_s=elapsed,
    )


def _probe_name():
    # type: () -> str
    """A dotfile name no concurrent run can collide with."""
    suffix = format(int.from_bytes(os.urandom(4), "big"), "08x")
    return ".dirscape-probe-%d-%s" % (os.getpid(), suffix)


def _write_errno_verdict(exc, elapsed, source):
    # type: (OSError, float, str) -> Verdict
    """Turn a failed write attempt into the right kind of verdict."""
    code = getattr(exc, "errno", None)
    detail = exc.strerror or str(exc)
    if code in (errno.EACCES, errno.EPERM, errno.EROFS):
        return refuted(VerdictCategory.ACCESS_DENIED, detail, source=source, elapsed_s=elapsed)
    if code in (errno.EDQUOT, errno.ENOSPC):
        # Writable but full is a different answer from denied, and the user
        # needs the difference: one is a permission problem, the other is a
        # quota problem and the fix is not the same.
        return refuted(VerdictCategory.QUOTA_EXCEEDED, detail, source=source, elapsed_s=elapsed)
    if code == errno.ENOENT:
        return refuted(VerdictCategory.NOT_PRESENT, detail, source=source, elapsed_s=elapsed)
    return unknown(VerdictCategory.BACKEND_FAILED, detail, source=source, elapsed_s=elapsed)


def probe_writable(path, allow_write=False, uid=None, deadline_s=DEFAULT_DEADLINE_S):
    # type: (str, bool, Optional[int], Optional[float]) -> Verdict
    """Whether the caller can write into a directory.

    The default answers only when it can do so unambiguously, and otherwise
    returns ``NOT_PROBED``. The reason is that **`os.access(W_OK)` lies under
    root-squashed NFS**: the client evaluates the mode bits locally and the
    server then refuses the write, so the answer is yes and the write fails.
    That configuration does not exist on this cluster and does exist at other
    sites, which makes it precisely the kind of thing to be careful about rather
    than to discover from a bug report.

    An owner match resolves the ambiguity, because squashing remaps uid 0 and
    nothing else, so "W_OK is true and I own it" cannot be the lie. A negative
    from ``os.access`` is trusted as durable: the measured 100% agreement with
    ``listdir`` covered the ACL cases, and a false negative would require the
    kernel to refuse a check it will later allow.

    With ``allow_write=True`` the question is settled by actually writing.
    ``Verdict.source`` records which method answered, because "O_TMPFILE said
    yes" and "os.access said yes" are not equally strong evidence and a reader
    of the JSON needs to know which one they have.
    """
    if uid is None:
        uid = os.getuid()

    if not allow_write:

        def probe():
            # type: () -> Tuple[bool, Optional[int]]
            writable = os.access(path, os.W_OK)
            owner = None  # type: Optional[int]
            try:
                owner = os.stat(path).st_uid
            except OSError:
                owner = None
            return (writable, owner)

        finished, value, exc, elapsed = with_deadline(probe, deadline_s)
        if not finished:
            return unknown(
                VerdictCategory.PROBE_TIMEOUT,
                _timeout_reason(deadline_s),
                source="os.access",
                elapsed_s=elapsed,
            )
        if exc is not None:
            return unknown(
                VerdictCategory.BACKEND_FAILED,
                "write check failed: %s" % (exc,),
                source="os.access",
                elapsed_s=elapsed,
            )

        writable, owner = value  # type: ignore[misc]
        if not writable:
            return refuted(
                VerdictCategory.ACCESS_DENIED,
                "os.access reports no write permission",
                source="os.access",
                elapsed_s=elapsed,
            )
        if uid != 0 and owner is not None and owner == uid:
            return confirmed(
                "os.access reports write, and the caller owns the directory",
                source="os.access",
                elapsed_s=elapsed,
            )
        return unknown(
            VerdictCategory.NOT_PROBED,
            "os.access reports write but the caller is not the owner; "
            "not trusted without a real write, because W_OK can be wrong under "
            "a root-squashed export",
            source="os.access",
            elapsed_s=elapsed,
        )

    # -- allow_write: settle it by writing --------------------------------

    def attempt():
        # type: () -> Tuple[str, Optional[OSError]]
        # O_TMPFILE first: it never creates a visible name, so an abandoned
        # probe cannot leave litter in somebody's project directory.
        try:
            handle = os.open(path, os.O_TMPFILE | os.O_WRONLY, 0o600)
        except OSError as exc:
            tmpfile_error = exc
        else:
            os.close(handle)
            return ("O_TMPFILE", None)

        # GPFS, and most network filesystems, answer O_TMPFILE with
        # EOPNOTSUPP. Fall back to a uniquely named dotfile, unlinked in a
        # finally inside this thread so even a probe the caller has already
        # abandoned on a deadline still cleans up after itself.
        if tmpfile_error.errno in (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT):
            # A permission or existence failure is the answer, not a reason to
            # try a second method.
            return ("O_TMPFILE", tmpfile_error)

        target = os.path.join(path, _probe_name())
        handle = -1
        try:
            handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            return ("dotfile", exc)
        finally:
            if handle >= 0:
                with contextlib.suppress(OSError):
                    os.close(handle)
            with contextlib.suppress(OSError):
                os.unlink(target)
        return ("dotfile", None)

    finished, value, exc, elapsed = with_deadline(attempt, deadline_s)
    if not finished:
        return unknown(
            VerdictCategory.PROBE_TIMEOUT,
            _timeout_reason(deadline_s),
            source="write probe",
            elapsed_s=elapsed,
        )
    if exc is not None:
        return unknown(
            VerdictCategory.BACKEND_FAILED,
            "write probe failed: %s" % (exc,),
            source="write probe",
            elapsed_s=elapsed,
        )

    method, error = value  # type: ignore[misc]
    if error is None:
        return confirmed("wrote and removed a probe file", source=method, elapsed_s=elapsed)
    return _write_errno_verdict(error, elapsed, method)


def symlink_info(path):
    # type: (str) -> Optional[SymlinkInfo]
    """Resolve one level of symlink and report where it lands.

    Returns ``None`` when ``path`` is not a symlink.

    ``crosses_device`` compares the ``st_dev`` of the link's parent with the
    ``st_dev`` of the target. **Measured caveat: that comparison is False for the
    case this field is usually explained with.** ``~/.cache ->
    /project/hpc/jdoe42/.cache`` crosses from the ``home`` fileset to the
    ``project-hpc`` fileset and therefore changes which quota the bytes land in,
    but ``/home`` and ``/project`` are two mountpoints of one GPFS device here,
    both ``st_dev`` 54, so the device test cannot see it. ``st_dev`` is
    reported because it is the strongest thing a single ``stat`` pair can say;
    `candidates.discover` sets ``Root.crosses_boundary`` from the enclosing
    MOUNTPOINT instead, which does separate the two.
    """
    try:
        if not os.path.islink(path):
            return None
        target = os.readlink(path)
    except OSError:
        return None

    parent = os.path.dirname(os.path.abspath(path))
    resolved = target if os.path.isabs(target) else os.path.normpath(os.path.join(parent, target))

    link_dev = None  # type: Optional[int]
    target_dev = None  # type: Optional[int]
    is_dir = False
    try:
        link_dev = os.stat(parent).st_dev
    except OSError:
        link_dev = None
    try:
        target_stat = os.stat(resolved)
        target_dev = target_stat.st_dev
        is_dir = os.path.isdir(resolved)
    except OSError:
        target_dev = None

    crosses = bool(link_dev is not None and target_dev is not None and link_dev != target_dev)
    return SymlinkInfo(
        path=path,
        target=target,
        resolved=resolved,
        is_dir=is_dir,
        crosses_device=crosses,
        link_dev=link_dev,
        target_dev=target_dev,
    )
