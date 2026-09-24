"""Unit tests for the gNMI prefix-origin workaround.

These tests are offline -- they assert on the constructed protobuf messages and
never talk to a device.
"""

import time

import pygnmi.client
import pytest
from pygnmi.client import gnmi_path_generator, gNMIException
from pygnmi.spec.v080.gnmi_pb2 import GetRequest as RawGetRequest
from pygnmi.spec.v080.gnmi_pb2 import Path, TypedValue, Update

# Importing the package installs the workaround as a side effect.
import GNMI  # noqa: F401
from GNMI import _prefix_workaround


def _build_get(prefix_str, path_strs):
    """Build a GetRequest the same way pygnmi's client.get() does."""
    return pygnmi.client.GetRequest(
        prefix=gnmi_path_generator(prefix_str),
        path=[gnmi_path_generator(p) for p in path_strs],
        encoding=4,
    )


def build_get(prefix_str, path_strs):
    """Build a GetRequest with the merge workaround enabled."""
    return _prefix_workaround.with_merge_mode(_build_get, True)(prefix_str, path_strs)


def build_raw(request_cls, **kwargs):
    """Build any pygnmi request with the merge workaround enabled."""
    return _prefix_workaround.with_merge_mode(request_cls, True)(**kwargs)


def test_prefix_field_is_omitted_when_prefix_supplied():
    req = build_get("Cisco-IOS-XR-um-logging-cfg:", ["/logging"])
    assert req.HasField("prefix") is False


def test_prefix_field_is_omitted_when_prefix_empty():
    """The empty-prefix case is rejected by XR 25.4.2 too, so it must also go."""
    req = build_get("", ["Cisco-IOS-XR-um-logging-cfg:logging"])
    assert req.HasField("prefix") is False


def test_origin_moves_into_path():
    req = build_get("Cisco-IOS-XR-um-logging-cfg:", ["/logging"])
    assert len(req.path) == 1
    assert req.path[0].origin == "Cisco-IOS-XR-um-logging-cfg"
    assert [e.name for e in req.path[0].elem] == ["logging"]


def test_prefix_and_no_prefix_forms_are_wire_identical():
    """The whole point: both spellings must produce the same bytes."""
    a = build_get("Cisco-IOS-XR-um-logging-cfg:", ["/logging"])
    b = build_get("", ["Cisco-IOS-XR-um-logging-cfg:logging"])
    assert a.SerializeToString() == b.SerializeToString()


def test_list_keys_with_slashes_survive():
    """Regression: string-based joining mangles keys containing '/' and '='."""
    req = build_get(
        "openconfig-interfaces:",
        ["/interfaces/interface[name=GigabitEthernet0/0/0/0]/state/counters"],
    )
    elems = req.path[0].elem
    assert [e.name for e in elems] == ["interfaces", "interface", "state", "counters"]
    assert dict(elems[1].key) == {"name": "GigabitEthernet0/0/0/0"}
    assert req.path[0].origin == "openconfig-interfaces"


def test_multi_element_prefix_is_prepended_not_concatenated():
    """Regression: naive string join produces a single 'interfaces:interface' elem."""
    req = build_get("openconfig-interfaces:/interfaces", ["interface"])
    assert [e.name for e in req.path[0].elem] == ["interfaces", "interface"]


def test_path_origin_wins_over_prefix_origin():
    req = build_get("openconfig-interfaces:", ["openconfig-system:system"])
    assert req.path[0].origin == "openconfig-system"


def test_multiple_paths_all_get_the_prefix():
    req = build_get("openconfig-interfaces:/interfaces", ["interface", "state"])
    assert [[e.name for e in p.elem] for p in req.path] == [
        ["interfaces", "interface"],
        ["interfaces", "state"],
    ]


def test_target_is_inherited_from_prefix():
    prefix = Path(target="dev1", origin="openconfig-system")
    req = build_raw(pygnmi.client.GetRequest, prefix=prefix, path=[gnmi_path_generator("/system")], encoding=4)
    assert req.HasField("prefix") is False
    assert req.path[0].target == "dev1"


def test_class_attributes_are_still_reachable_through_the_proxy():
    """pygnmi calls GetRequest.DataType.Value(...); the proxy must forward it."""
    assert pygnmi.client.GetRequest.DataType.Value("CONFIG") == RawGetRequest.DataType.Value("CONFIG")


def test_set_request_delete_paths_are_merged():
    req = build_raw(
        pygnmi.client.SetRequest,
        prefix=gnmi_path_generator("openconfig-interfaces:/interfaces"),
        delete=[gnmi_path_generator("interface[name=Gi0/0/0/0]")],
    )
    assert req.HasField("prefix") is False
    assert [e.name for e in req.delete[0].elem] == ["interfaces", "interface"]
    assert dict(req.delete[0].elem[1].key) == {"name": "Gi0/0/0/0"}


@pytest.mark.parametrize("field", ["update", "replace"])
def test_set_request_update_and_replace_paths_are_merged(field):
    upd = Update(
        path=gnmi_path_generator("interface[name=Gi0/0/0/0]"),
        val=TypedValue(json_ietf_val=b"{}"),
    )
    req = build_raw(
        pygnmi.client.SetRequest,
        prefix=gnmi_path_generator("openconfig-interfaces:/interfaces"),
        **{field: [upd]},
    )
    assert req.HasField("prefix") is False
    entry = getattr(req, field)[0]
    assert entry.path.origin == "openconfig-interfaces"
    assert [e.name for e in entry.path.elem] == ["interfaces", "interface"]
    assert entry.val.json_ietf_val == b"{}", "payload must be preserved"


def test_default_is_off_so_the_prefix_field_is_untouched():
    """Merging is opt-in: with no mode set, stock pygnmi behaviour applies."""
    req = _build_get("Cisco-IOS-XR-um-logging-cfg:", ["/logging"])
    assert req.HasField("prefix") is True
    assert req.prefix.origin == "Cisco-IOS-XR-um-logging-cfg"
    assert req.path[0].origin == ""


def test_merge_mode_false_leaves_the_prefix_field_in_place():
    captured = {}

    def build():
        captured["req"] = _build_get("Cisco-IOS-XR-um-logging-cfg:", ["/logging"])

    _prefix_workaround.with_merge_mode(build, False)()
    assert captured["req"].HasField("prefix") is True


def test_merge_mode_true_drops_the_prefix_field():
    captured = {}

    def build():
        captured["req"] = _build_get("Cisco-IOS-XR-um-logging-cfg:", ["/logging"])

    _prefix_workaround.with_merge_mode(build, True)()
    assert captured["req"].HasField("prefix") is False


def test_merge_mode_is_restored_after_the_call():
    assert _prefix_workaround.merge_enabled() is False
    _prefix_workaround.with_merge_mode(lambda: None, True)()
    assert _prefix_workaround.merge_enabled() is False


def test_merge_mode_is_restored_even_if_the_call_raises():
    def boom():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _prefix_workaround.with_merge_mode(boom, True)()
    assert _prefix_workaround.merge_enabled() is False


class _FakeSession:
    """Records the prefix mode that was active when get()/set() ran."""

    def __init__(self):
        self.seen = None

    def _record(self, **kwargs):
        self.seen = _prefix_workaround.merge_enabled()
        return {"notification": []}

    get = _record
    set = _record


@pytest.mark.parametrize(
    ("session_default", "call_override", "expected"),
    [
        (False, None, False),  # default: workaround off
        (True, None, True),  # session opt-in inherited
        (False, True, True),  # per-call opt-in
        (True, False, False),  # per-call opt-out beats session
    ],
)
@pytest.mark.parametrize("keyword_name", ["get", "set_"])
def test_merge_prefix_argument_plumbing(session_default, call_override, expected, keyword_name):
    lib = GNMI.GNMI()
    fake = _FakeSession()
    lib.sessions["s"] = fake
    lib.merge_prefix_into_path["s"] = session_default

    getattr(lib, keyword_name)("s", merge_prefix_into_path=call_override)
    assert fake.seen is expected


def test_default_session_does_not_merge():
    """A session connected without the argument must behave like stock pygnmi."""
    lib = GNMI.GNMI()
    fake = _FakeSession()
    lib.sessions["s"] = fake
    lib.get("s")
    assert fake.seen is False


@pytest.mark.parametrize("timeout", [None, 30])
def test_merge_prefix_applies_on_both_timeout_paths(timeout):
    """_run_with_timeout runs inline when timeout is None and in a worker thread
    otherwise; the thread-local must be set correctly in both cases."""
    lib = GNMI.GNMI()
    fake = _FakeSession()
    lib.sessions["s"] = fake
    lib.get("s", merge_prefix_into_path=True, timeout=timeout)
    assert fake.seen is True


def test_merge_helper_is_pure():
    prefix = gnmi_path_generator("openconfig-interfaces:/interfaces")
    path = gnmi_path_generator("interface")
    merged = _prefix_workaround.merge_prefix_into_path(prefix, path)
    assert [e.name for e in merged.elem] == ["interfaces", "interface"]
    # inputs untouched
    assert [e.name for e in prefix.elem] == ["interfaces"]
    assert [e.name for e in path.elem] == ["interface"]


def test_applying_twice_is_idempotent():
    before = pygnmi.client.GetRequest
    _prefix_workaround.apply()
    assert pygnmi.client.GetRequest is before, "must not double-wrap"


class _FailingSession:
    """Raises the origin-mismatch error the workaround exists for."""

    MESSAGE = (
        "GRPC ERROR Host: 10.0.0.1:57400, Error: gNMI: invalid YangGet: "
        "rpc error: code = Internal desc = prefix and path origins do not match"
    )

    def _fail(self, **kwargs):
        raise gNMIException(self.MESSAGE)

    get = _fail
    set = _fail


@pytest.mark.parametrize("keyword_name", ["get", "set_"])
def test_origin_mismatch_error_gains_a_hint_when_workaround_is_off(keyword_name):
    lib = GNMI.GNMI()
    lib.sessions["s"] = _FailingSession()

    with pytest.raises(gNMIException) as excinfo:
        getattr(lib, keyword_name)("s")

    message = str(excinfo.value)
    assert "prefix and path origins do not match" in message, "original error preserved"
    assert "merge_prefix_into_path" in message, "hint names the argument that fixes it"
    assert excinfo.value.__cause__ is not None, "original exception chained"


@pytest.mark.parametrize("keyword_name", ["get", "set_"])
def test_no_hint_when_the_workaround_is_already_on(keyword_name):
    """If merging is already enabled the hint would be wrong advice."""
    lib = GNMI.GNMI()
    lib.sessions["s"] = _FailingSession()

    with pytest.raises(gNMIException) as excinfo:
        getattr(lib, keyword_name)("s", merge_prefix_into_path=True)

    assert "merge_prefix_into_path" not in str(excinfo.value)


def test_unrelated_errors_are_re_raised_untouched():
    class Boom:
        def get(self, **kwargs):
            raise ValueError("something else entirely")

    lib = GNMI.GNMI()
    lib.sessions["s"] = Boom()

    with pytest.raises(ValueError, match="something else entirely"):
        lib.get("s")


def test_timeout_errors_are_not_mistaken_for_origin_mismatch():
    class Slow:
        def get(self, **kwargs):
            time.sleep(2)

    lib = GNMI.GNMI()
    lib.sessions["s"] = Slow()

    with pytest.raises(TimeoutError):
        lib.get("s", timeout=1)


def test_marker_detection():
    assert _prefix_workaround.is_origin_mismatch_error(Exception(_FailingSession.MESSAGE))
    assert not _prefix_workaround.is_origin_mismatch_error(Exception("unrelated"))
