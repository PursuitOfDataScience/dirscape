"""`dirscape mcp`: the protocol, the tools, and the stream kept clean.

A client that meets one malformed line usually drops the connection, and the
model on the other end is then told only that the tool "failed". So the tests
here are mostly about the protocol's edges rather than the answers, which
`test_agent.py` owns: a notification is never answered, a tool failure is a
result the model can read, a stray print cannot reach stdout, and the whole
exchange runs on the bare `/usr/bin/python3` of a login node.

The sweep is injected everywhere, so nothing here touches the cluster.
"""

import io
import json
import os
import subprocess
import sys

import pytest

# The fixture cluster, shared on purpose so both files describe one machine.
from test_agent import _run

from dirscape import cli, mcp

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class Sweeps(object):
    """A sweeper that counts, so the cache can be tested."""

    def __init__(self):
        self.calls = []

    def __call__(self, since=None):
        self.calls.append(since)
        return _run()


def _server(clock=None):
    sweeps = Sweeps()
    kwargs = {"clock": clock} if clock is not None else {}
    return mcp.Server(sweeps, **kwargs), sweeps


def _ask(server, method, params=None, ident=1):
    message = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        message["params"] = params
    return server.receive(json.dumps(message).encode("utf-8"))


def _call(server, name, arguments=None):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return _ask(server, "tools/call", params)["result"]


# --------------------------------------------------------------------------
# Handshake
# --------------------------------------------------------------------------


@pytest.mark.parametrize("version", mcp.PROTOCOL_VERSIONS)
def test_a_known_protocol_version_is_echoed(version):
    server, _ = _server()
    result = _ask(server, "initialize", {"protocolVersion": version, "capabilities": {}})
    assert result["result"]["protocolVersion"] == version
    assert result["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert result["result"]["serverInfo"]["name"] == "dirscape"


def test_an_unknown_version_gets_the_newest_and_the_client_decides():
    server, _ = _server()
    result = _ask(server, "initialize", {"protocolVersion": "1999-01-01"})
    assert result["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]


def test_the_instructions_teach_the_two_marks():
    server, _ = _server()
    text = _ask(server, "initialize", {"protocolVersion": "2025-06-18"})["result"]["instructions"]
    assert "list_paths" in text and "`?`" in text and "`none`" in text


def test_a_notification_is_never_answered():
    server, _ = _server()
    for method in ("notifications/initialized", "notifications/cancelled", "no/such/thing"):
        line = json.dumps({"jsonrpc": "2.0", "method": method}).encode("utf-8")
        assert server.receive(line) is None, method


def test_ping_and_an_unknown_method():
    server, _ = _server()
    assert _ask(server, "ping")["result"] == {}
    assert _ask(server, "resources/list")["error"]["code"] == mcp.METHOD_NOT_FOUND


def test_junk_on_the_wire_is_a_parse_error_not_a_crash():
    server, _ = _server()
    assert server.receive(b"{not json")["error"]["code"] == mcp.PARSE_ERROR
    assert server.receive(b"\xff\xfe")["error"]["code"] == mcp.PARSE_ERROR
    assert server.receive(b'{"id": 3, "method": "ping"}')["error"]["code"] == mcp.INVALID_REQUEST
    assert server.receive(b"[]")["error"]["code"] == mcp.INVALID_REQUEST


def test_a_batch_answers_its_requests_and_not_its_notifications():
    server, _ = _server()
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    replies = server.receive(json.dumps(batch).encode("utf-8"))
    assert [reply["id"] for reply in replies] == [1, 2]


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def test_every_tool_is_read_only_and_its_schema_is_closed():
    server, _ = _server()
    tools = _ask(server, "tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "list_paths",
        "explain_path",
        "recover_path",
        "list_changes",
    ]
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema.get("required", ())) <= set(schema["properties"])
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert len(tool["description"]) > 80, "the description is the model's whole manual"


def test_list_paths_carries_the_payload_twice_for_a_new_client():
    server, _ = _server()
    _ask(server, "initialize", {"protocolVersion": "2025-06-18"})
    result = _call(server, "list_paths", {"writable": True})
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert all(p["can_write"] is True for p in result["structuredContent"]["paths"])


def test_an_old_client_gets_text_only():
    server, _ = _server()
    _ask(server, "initialize", {"protocolVersion": "2024-11-05"})
    result = _call(server, "list_paths")
    assert "structuredContent" not in result
    assert json.loads(result["content"][0]["text"])["view"] == "paths"


@pytest.mark.parametrize(
    "arguments, said",
    [
        ({"writeable": True}, "unknown argument writeable"),
        ({"kind": "scrach"}, "'scrach' is not a kind"),
        ({"min_free": "lots"}, "is not a size"),
        ({"min_free": True}, "must be a size"),
        ({"all": "yes"}, "must be true or false"),
    ],
)
def test_a_bad_argument_is_a_result_the_model_can_read(arguments, said):
    """`isError`, so the model sees the sentence and can correct itself."""
    server, sweeps = _server()
    result = _call(server, "list_paths", arguments)
    assert result["isError"] is True
    assert said in result["content"][0]["text"]
    assert sweeps.calls == [], "nothing is measured for a call that cannot be answered"


def test_a_relative_path_is_refused_rather_than_read_from_the_servers_directory():
    """The server's working directory is the client's choice, not the agent's."""
    server, sweeps = _server()
    for name in ("explain_path", "recover_path"):
        result = _call(server, name, {"path": "data/run42"})
        assert result["isError"] is True and "must be absolute" in result["content"][0]["text"]
    assert sweeps.calls == []


def test_kind_may_come_as_a_list():
    server, _ = _server()
    listed = _call(server, "list_paths", {"kind": ["home", "scratch"]})["structuredContent"]
    joined = _call(server, "list_paths", {"kind": "home,scratch"})["structuredContent"]
    assert listed["paths"] == joined["paths"] and [p["kind"] for p in listed["paths"]] == [
        "home",
        "scratch",
    ]


def test_an_unknown_tool_is_a_protocol_error():
    server, _ = _server()
    reply = _ask(server, "tools/call", {"name": "delete_everything", "arguments": {}})
    assert reply["error"]["code"] == mcp.INVALID_PARAMS
    assert "list_paths" in reply["error"]["message"]


def test_explain_path_with_no_root_above_says_whys_sentence():
    run = _run()
    run.roots = [root for root in run.roots if root.path != "/"]
    server = mcp.Server(lambda since=None: run)
    result = _call(server, "explain_path", {"path": "/definitely/not/here"})
    assert result["isError"] is True
    assert "does not exist" in result["content"][0]["text"]


def test_explain_path_answers_where_a_new_path_would_land():
    """Under `/`, which is read-only here: the honest answer to "can I write it"."""
    server, _ = _server()
    answer = _call(server, "explain_path", {"path": "/definitely/not/here"})["structuredContent"]
    assert (answer["exists"], answer["root"], answer["nearest_existing"]) == (False, "/", "/")
    assert answer["place"]["can_write"] is False


def test_a_path_is_required_where_one_is_needed():
    server, _ = _server()
    for name in ("explain_path", "recover_path"):
        result = _call(server, name, {})
        assert result["isError"] is True and "`path` is required" in result["content"][0]["text"]


def test_a_tool_that_breaks_is_reported_and_the_server_survives():
    server, _ = _server()

    def boom(since=None):
        raise RuntimeError("the mount table went away")

    server._sweeper = boom
    result = _call(server, "list_paths")
    assert result["isError"] is True and "the mount table went away" in result["content"][0]["text"]
    assert _ask(server, "ping")["result"] == {}


def test_list_changes_takes_a_duration_and_refuses_anything_else():
    server, sweeps = _server()
    assert _call(server, "list_changes", {"since": "soon"})["isError"] is True
    assert sweeps.calls == []
    assert _call(server, "list_changes", {"since": "30d"})["isError"] is False
    assert sweeps.calls == ["30d"], "a different baseline is a sweep of its own"


def test_list_changes_with_tracking_off_says_so():
    server, _ = _server()
    payload = _call(server, "list_changes")["structuredContent"]
    assert payload["tracking"] is False and payload["changes"] == []


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------


def test_one_sweep_answers_a_burst_and_refresh_or_age_measures_again():
    now = [1000.0]
    server, sweeps = _server(clock=lambda: now[0])
    _call(server, "list_paths")
    _call(server, "list_paths", {"kind": "home"})
    assert len(sweeps.calls) == 1, "the second question reuses the first sweep"
    _call(server, "list_paths", {"refresh": True})
    assert len(sweeps.calls) == 2
    now[0] += mcp.CACHE_SECONDS + 1
    _call(server, "list_paths")
    assert len(sweeps.calls) == 3, "an old answer is measured again"


# --------------------------------------------------------------------------
# The stream
# --------------------------------------------------------------------------


def test_nothing_but_protocol_reaches_stdout(capsys):
    """A backend that prints is a line the client cannot parse."""

    def chatty(since=None):
        sys.stdout.write("a backend talking out of turn\n")
        return _run()

    server = mcp.Server(chatty)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_paths"}},
    ]
    stdin = io.BytesIO(b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in lines))
    stdout = io.BytesIO()
    assert mcp.serve(None, stdin=stdin, stdout=stdout, server=server) == 0

    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2]
    assert "talking out of turn" in capsys.readouterr().err
    assert sys.stdout is not sys.stderr, "stdout is put back afterwards"


def test_every_reply_is_one_ascii_line():
    server, _ = _server()
    _ask(server, "initialize", {"protocolVersion": "2025-06-18"})
    reply = mcp._dumps(_ask(server, "tools/call", {"name": "list_paths"}))
    assert "\n" not in reply
    reply.encode("ascii")


def test_the_cli_serves_without_sweeping_first(monkeypatch):
    """`main` hands over before the sweep, which the server runs per request."""
    monkeypatch.setattr(cli, "sweep", lambda *a, **k: pytest.fail("main must not sweep"))
    served = []
    monkeypatch.setattr(mcp, "serve", lambda opts: served.append(opts) or 0)
    assert cli.main(["mcp"]) == 0
    assert served


# --------------------------------------------------------------------------
# The login-node interpreter
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.path.exists("/usr/bin/python3"), reason="no system python3 to test against"
)
def test_the_handshake_runs_on_the_system_python():
    """The server has to run where the package does, which includes 3.6.

    Only the calls that need no sweep, so this measures the protocol and the
    imports, not the cluster it happens to run on.
    """
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    env = dict(os.environ, PYTHONPATH=SRC)
    proc = subprocess.run(
        ["/usr/bin/python3", "-m", "dirscape", "mcp"],
        input=b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in lines),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    assert len(replies[1]["result"]["tools"]) == len(mcp.TOOLS)
