"""`dirscape mcp`: the same answers, as tools an agent calls natively.

Model Context Protocol over stdio: newline-delimited JSON-RPC 2.0 on stdin
and stdout, which is the whole transport, so it needs nothing outside the
standard library and runs on the same bare `/usr/bin/python3` as the rest of
the package. An MCP SDK would have been a first runtime dependency for a tool
whose selling point is having none.

Register it once and every session has the tools:

    claude mcp add --scope user dirscape -- dirscape mcp

Four tools, all read-only. Each answer is `agent.py`'s, so an agent calling a
tool and a person running `dirscape paths --json` are handed the same record:

    list_paths      the table's rows, with exact figures
    explain_path    one path: which place governs it, and why
    recover_path    snapshot copies of a path, and the command to restore
    list_changes    what changed since the baseline

Three rules the protocol does not make obvious, each of which breaks a client
when it is got wrong:

1. **Nothing but protocol messages reaches stdout.** One stray `print` is a
   line the client cannot parse, and several clients drop the connection on
   it. `sys.stdout` is pointed at stderr for the life of the server and the
   real stream is written only here.
2. **A notification is never answered**, not even with an error.
3. **A tool that fails returns `isError`, not a protocol error.** The model
   reads the message and can correct itself ("that path does not exist, its
   parent is ..."); a protocol error is swallowed by the client and the model
   only learns that something went wrong.

A sweep costs two to five seconds, so one is reused for `CACHE_SECONDS`: an
agent that lists the storage and then asks about three paths pays for one.
"""

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__

__all__ = ["CACHE_SECONDS", "PROTOCOL_VERSIONS", "TOOLS", "Server", "serve"]

#: Newest first. An `initialize` naming one of these gets it back; any other
#: gets the newest, and the client decides whether it can speak that, which is
#: the negotiation the specification describes.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

#: `structuredContent` arrived in this revision. Older clients are sent the
#: same JSON as text only, which the specification asks for anyway.
STRUCTURED_SINCE = "2025-06-18"

#: How long one sweep answers for. Long enough to cover a burst of questions
#: from one task, short enough that the next task measures afresh. Every tool
#: takes `refresh` for the case in between: an agent that has just written
#: 50G somewhere should not be told the old figure.
CACHE_SECONDS = 60.0

_INSTRUCTIONS = (
    "dirscape reports the storage this user can reach on this machine: home, project, "
    "scratch, datasets, software and node-local disks, with access, usage, quota and free "
    "space for each. Call list_paths first. Figures come twice: `used`, `quota`, `free` "
    "are the text a person sees in `dirscape why` (`?` means nobody could measure it and is "
    "never zero; `none` means no limit is enforced), and `used_bytes`, `quota_bytes`, "
    "`free_bytes` are exact, null where the text is `?` or `none`. `quota_scope` other "
    "than `user` means the figure counts everyone sharing that quota. Use explain_path "
    "before writing somewhere: it names the place a path bills to, including paths that do "
    "not exist yet. Mounts differ between login and compute nodes, so an answer is about "
    "the node in `host` and `node_class`. Everything is read-only; answers are reused for "
    "%d seconds unless refresh is true." % (CACHE_SECONDS,)
)

_REFRESH = {
    "type": "boolean",
    "description": "Measure again instead of reusing an answer up to %d seconds old. Use "
    "it after writing or deleting a lot of data." % (CACHE_SECONDS,),
}

_PATH = {
    "type": "string",
    "description": "An absolute path, or one starting with ~. It may be a file, and for "
    "explain_path it need not exist yet.",
}

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    # Nothing leaves this machine: every answer comes from the local mount
    # table, local quota tools and the local filesystem.
    "openWorldHint": False,
}  # type: Dict[str, Any]

TOOLS = [
    {
        "name": "list_paths",
        "title": "Storage you can use",
        "description": (
            "List the places this user can put data on this machine (home, project, scratch, "
            "dataset, software, archive, local), one record each: path, kind, access words "
            "and can_read/can_write, used/quota/free as table text plus exact bytes, file "
            "counts and file limits, purge_days, retention, snapshot count, and symlinked_to "
            "(other places whose quota this one's symlinked folders fill). Also returns "
            "`stranded` (space you are charged for in filesets you cannot reach), `elsewhere` "
            "(allocations with no path on this machine) and `hidden`, the number of roots "
            "folded out of the list. Filter with writable, kind and min_free."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "all": {
                    "type": "boolean",
                    "description": "Every root found, including filesystem roots, aliases "
                    "and subdirectories of a listed place. Default false: the table's rows.",
                },
                "writable": {
                    "type": "boolean",
                    "description": "Only places where writing was confirmed.",
                },
                "kind": {
                    "type": "string",
                    "description": "Only these kinds, comma-separated: home, project, "
                    "scratch, dataset, software, archive, local, other.",
                },
                "min_free": {
                    "type": "string",
                    "description": "Only places with at least this much free space, e.g. "
                    "500G or 2T (binary units, as du -h prints them) or a byte count. A place "
                    "whose free space is unknown is left out.",
                },
                "refresh": _REFRESH,
            },
            "additionalProperties": False,
        },
        "annotations": dict(_READ_ONLY, title="Storage you can use"),
    },
    {
        "name": "explain_path",
        "title": "Explain one path",
        "description": (
            "Explain one file or directory: the place that governs it (`root`), and that "
            "place's record with the detail layer added: where the figures came from "
            "(`source`), how write access was checked, the newest snapshot and a `restore` "
            "command, symlinks out of it, and why any figure is unknown (`unknown`). A path "
            "that does not exist yet is answered for the place it would be created in "
            "(`exists` false), so call this before writing output somewhere. When the quota "
            "is reported on a parent place, `quota_from` carries that place's figures."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": _PATH, "refresh": _REFRESH},
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": dict(_READ_ONLY, title="Explain one path"),
    },
    {
        "name": "recover_path",
        "title": "Find snapshot copies",
        "description": (
            "Find the read-only snapshot copies the filesystem still keeps of a file or "
            "directory, newest first, including one that has already been deleted, and the "
            "`restore` command (a cp that puts back what is missing without overwriting newer "
            "files). No copies with `recoverable.value` false means none are kept; unknown "
            "means no snapshot mechanism was found, which is not the same as no backup."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": _PATH},
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": dict(_READ_ONLY, title="Find snapshot copies"),
    },
    {
        "name": "list_changes",
        "title": "What changed",
        "description": (
            "What changed since the last baseline dirscape recorded on this cluster: places "
            "that are new, gone, opened or closed to you, or grew or shrank. Read-only: it "
            "never records a baseline itself, so it cannot hide a change from the person who "
            "runs dirscape next. `no_baseline` true means there is nothing to compare against "
            "yet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "Compare against the newest baseline at least this old, "
                    "e.g. 30d, 12h, 2w. Default: the most recent baseline.",
                }
            },
            "additionalProperties": False,
        },
        "annotations": dict(_READ_ONLY, title="What changed"),
    },
]  # type: List[Dict[str, Any]]

_BY_NAME = {tool["name"]: tool for tool in TOOLS}  # type: Dict[str, Dict[str, Any]]

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """A failure the model should read and act on, returned as `isError`."""


def _error(ident, code, message):
    # type: (object, int, str) -> Dict[str, Any]
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _result(ident, result):
    # type: (object, Dict[str, Any]) -> Dict[str, Any]
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _flag(arguments, name):
    # type: (Dict[str, Any], str) -> bool
    value = arguments.get(name, False)
    if not isinstance(value, bool):
        raise ToolError("`%s` must be true or false, not %r" % (name, value))
    return value


def _text(arguments, name, required=False):
    # type: (Dict[str, Any], str, bool) -> Optional[str]
    value = arguments.get(name)
    if value is None:
        if required:
            raise ToolError("`%s` is required" % (name,))
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ToolError("`%s` must be a non-empty string, not %r" % (name, value))
    return value


def _path(arguments):
    # type: (Dict[str, Any]) -> str
    """The `path` argument, absolute or `~`, never relative.

    A relative path would resolve against THIS process's directory, which is
    wherever the client happened to start the server and not wherever the
    agent thinks it is standing, so `data/run42` could be answered for a
    directory the agent never meant.
    """
    path = str(_text(arguments, "path", required=True)).strip()
    if not path.startswith(("/", "~")):
        raise ToolError(
            "`path` must be absolute or start with ~, not %r: a relative path would be "
            "read from the server's directory, %s, which need not be yours" % (path, os.getcwd())
        )
    return path


def _kinds(arguments):
    # type: (Dict[str, Any]) -> Tuple[str, ...]
    """`kind` as the schema says, a comma-separated string, or as a list.

    A model that has seen an array of kinds somewhere will send one, and
    refusing an unambiguous answer over its spelling helps nobody.
    """
    from . import agent

    value = arguments.get("kind")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return agent.parse_kinds(value)
    return agent.parse_kinds(_text(arguments, "kind"))


class Server(object):
    """The protocol, with the sweep handed in so it can be tested without one.

    ``sweeper(since)`` returns a `cli.Run`; ``clock`` is `time.time` unless a
    test needs to move it.
    """

    def __init__(self, sweeper, ttl=CACHE_SECONDS, clock=time.time):
        # type: (Callable[[Optional[str]], Any], float, Callable[[], float]) -> None
        self._sweeper = sweeper
        self._ttl = ttl
        self._clock = clock
        self._run = None  # type: Any
        self._at = 0.0
        self.protocol = PROTOCOL_VERSIONS[0]

    # -- the sweep ---------------------------------------------------------

    def run(self, refresh=False):
        # type: (bool) -> Any
        now = self._clock()
        if refresh or self._run is None or now - self._at >= self._ttl:
            self._run = self._sweeper(None)
            self._at = self._clock()
        return self._run

    # -- the tools ---------------------------------------------------------

    def call(self, name, arguments):
        # type: (str, Dict[str, Any]) -> Dict[str, Any]
        """Run one tool and return its payload. Raises ToolError."""
        from . import agent, cli

        schema = _BY_NAME[name]["inputSchema"]
        allowed = set(schema.get("properties", {}))
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            # Refused rather than ignored. A misspelt `writeable` silently
            # dropped is an unfiltered answer the model believes is filtered.
            raise ToolError(
                "unknown argument%s %s; %s takes %s"
                % (
                    "" if len(unknown) == 1 else "s",
                    ", ".join(unknown),
                    name,
                    ", ".join(sorted(allowed)) or "no arguments",
                )
            )

        if name == "list_paths":
            try:
                kinds = _kinds(arguments)
                raw = arguments.get("min_free")
                if isinstance(raw, bool):
                    raise ValueError("`min_free` must be a size such as 2T, not %r" % (raw,))
                min_free = agent.parse_size(raw) if raw not in (None, "") else None
            except ValueError as exc:
                raise ToolError(str(exc)) from None
            show_all = _flag(arguments, "all")
            writable = _flag(arguments, "writable")
            run = self.run(_flag(arguments, "refresh"))
            return agent.paths_payload(
                run, show_all=show_all, writable=writable, kinds=kinds, min_free=min_free
            )

        if name == "explain_path":
            path = _path(arguments)
            run = self.run(_flag(arguments, "refresh"))
            try:
                return agent.explain_payload(run, str(path))
            except agent.PathError as exc:
                raise ToolError(str(exc)) from None

        if name == "recover_path":
            path = _path(arguments)
            return agent.recover_payload(self.run(), str(path))

        if name == "list_changes":
            since = _text(arguments, "since")
            if since:
                try:
                    cli.parse_duration(since)
                except ValueError as exc:
                    raise ToolError(str(exc)) from None
                # A different baseline is a different diff, and the diff
                # labels the live roots as it goes, so it gets a sweep of its
                # own rather than relabelling the cached one.
                return agent.changes_payload(self._sweeper(since))
            return agent.changes_payload(self.run())

        raise ToolError("no tool named %s" % (name,))  # pragma: no cover - checked by caller

    # -- the protocol ------------------------------------------------------

    def _initialize(self, params):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        self.protocol = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": self.protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "dirscape", "title": "dirscape", "version": __version__},
            "instructions": _INSTRUCTIONS,
        }

    def _tools_call(self, ident, params):
        # type: (object, Any) -> Dict[str, Any]
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(ident, INVALID_PARAMS, "tools/call needs a tool `name`")
        name = params["name"]
        if name not in _BY_NAME:
            return _error(
                ident,
                INVALID_PARAMS,
                "unknown tool %r; the tools are %s" % (name, ", ".join(sorted(_BY_NAME))),
            )
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _result(ident, self._failed("`arguments` must be an object"))
        try:
            payload = self.call(name, arguments)
        except ToolError as exc:
            return _result(ident, self._failed(str(exc)))
        except Exception as exc:  # the model is told; the server survives
            return _result(
                ident, self._failed("dirscape failed: %s: %s" % (type(exc).__name__, exc))
            )
        result = {
            "content": [{"type": "text", "text": _dumps(payload)}],
            "isError": False,
        }  # type: Dict[str, Any]
        if self.protocol >= STRUCTURED_SINCE:
            result["structuredContent"] = payload
        return _result(ident, result)

    @staticmethod
    def _failed(message):
        # type: (str) -> Dict[str, Any]
        return {"content": [{"type": "text", "text": message}], "isError": True}

    def handle(self, message):
        # type: (Any) -> Optional[Dict[str, Any]]
        """One decoded message in, its response out, or None when none is owed."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            ident = message.get("id") if isinstance(message, dict) else None
            return _error(ident, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        if not isinstance(method, str):
            # A response to a request this server never sends, or junk with an
            # id. Nothing is owed either way.
            if "id" in message and "result" not in message and "error" not in message:
                return _error(message.get("id"), INVALID_REQUEST, "a request needs a `method`")
            return None
        notification = "id" not in message
        ident = message.get("id")
        params = message.get("params") or {}
        if notification:
            # `notifications/initialized`, `notifications/cancelled` and the
            # rest: nothing to do, and never anything to say.
            return None
        if method == "initialize":
            return _result(ident, self._initialize(params))
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            return _result(ident, {"tools": TOOLS})
        if method == "tools/call":
            return self._tools_call(ident, params)
        return _error(ident, METHOD_NOT_FOUND, "method not found: %s" % (method,))

    def receive(self, line):
        # type: (bytes) -> Any
        """One line off the wire: a message, or a batch of them."""
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return _error(None, PARSE_ERROR, "could not parse the message: %s" % (exc,))
        if isinstance(message, list):
            # Batches were in the 2025-03-26 revision and dropped after it;
            # answering one costs nothing and a client of that revision may
            # send one.
            if not message:
                return _error(None, INVALID_REQUEST, "an empty batch")
            replies = [self._safely(item) for item in message]
            replies = [reply for reply in replies if reply is not None]
            return replies or None
        return self._safely(message)

    def _safely(self, message):
        # type: (Any) -> Optional[Dict[str, Any]]
        try:
            return self.handle(message)
        except Exception as exc:
            ident = message.get("id") if isinstance(message, dict) else None
            if isinstance(message, dict) and "id" not in message:
                return None
            return _error(ident, INTERNAL_ERROR, "%s: %s" % (type(exc).__name__, exc))


def _dumps(payload):
    # type: (Any) -> str
    """Compact and ASCII: one line, whatever the locale of a login node is."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _sweeper(opts):
    # type: (argparse.Namespace) -> Callable[[Optional[str]], Any]
    """A sweep that reads the lineage and never writes it. See `cli.sweep`."""
    from . import cli

    def sweep(since=None):
        # type: (Optional[str]) -> Any
        namespace = argparse.Namespace(**vars(opts))
        namespace.since = since
        return cli.sweep(namespace, save_state=False)

    return sweep


def serve(opts, stdin=None, stdout=None, server=None):
    # type: (argparse.Namespace, Any, Any, Optional[Server]) -> int
    """Answer on ``stdout`` until ``stdin`` closes. Returns the exit code."""
    reader = stdin if stdin is not None else sys.stdin.buffer
    writer = stdout if stdout is not None else sys.stdout.buffer
    server = server or Server(_sweeper(opts))
    # Everything this process might print, warnings from a backend included,
    # goes to stderr, where a client logs it. See rule 1 at the top.
    saved = sys.stdout
    sys.stdout = sys.stderr
    try:
        for raw in iter(reader.readline, b""):
            line = raw.strip()
            if not line:
                continue
            reply = server.receive(line)
            if reply is None:
                continue
            writer.write(_dumps(reply).encode("ascii") + b"\n")
            writer.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        # The client went away. There is nobody left to tell.
        return 0
    finally:
        sys.stdout = saved
    return 0
