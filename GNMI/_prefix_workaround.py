"""Workaround for gNMI servers that reject a present ``prefix`` field whose
origin differs from the path origin.

Background
----------
The gNMI specification, section 2.7 ("gNMI Origin in ``Path``"), states:

    If more than one ``origin`` is to be used within any message, a path in the
    ``prefix`` MUST NOT be specified, since a prefix applies to all paths within
    the message. In the case that a ``prefix`` is specified, it MUST specify any
    required ``origin``. A single request MUST NOT specify ``origin`` in both
    ``prefix`` and ``path`` fields in any RPC payload messages.

IOS-XR 25.4.2 does not honour this. Whenever the ``prefix`` field is present on
the wire it requires ``prefix.origin == path.origin`` (plain string equality),
and rejects everything else with::

    rpc error: code = Internal desc = prefix and path origins do not match

Measured on IOS-XR 25.4.2 (24.4.2 and 26.2.1 are not affected)::

    prefix field   prefix.origin   path.origin   result
    -----------------------------------------------------------------------
    present        "module"        (unset)       FAIL  <- the spec-mandated form
    present        (unset)         "module"      FAIL  <- what pygnmi sends for prefix=""
    absent         --              "module"      PASS  <- spec-legal; used here
    present        "module"        "module"      PASS  <- spec-forbidden; not used

pygnmi always assigns ``prefix`` when it builds a ``GetRequest`` / ``SetRequest``.
Because ``prefix`` is a protobuf *message* field, assigning it marks the field
present on the wire even when it is completely empty (proto3 omits empty
*scalars*, not empty submessages). Consequently **every** origin-bearing request
issued through pygnmi is rejected by 25.4.2 -- whether or not the caller passed a
prefix of their own.

This module wraps request construction so that:

* a caller-supplied prefix is folded into each path (prepending its elements and
  inheriting its ``origin`` / ``target``), and
* the ``prefix`` field is omitted from the request entirely.

The result is semantically identical to the original request, remains
spec-compliant, and is accepted by affected and unaffected servers alike.

The merge is performed on the protobuf messages rather than on path *strings*,
so list keys (``interface[name=GigabitEthernet0/0/0/0]``) and multi-element
prefixes survive intact.

The workaround is enabled by default. It can be turned off per session or per
call with the ``keep_prefix`` argument of the ``GNMI connect session``,
``GNMI get`` and ``GNMI set`` keywords, which restores stock pygnmi behaviour.
"""

import threading
from typing import Any, Callable

import pygnmi.client

try:
    from pygnmi.spec.v080.gnmi_pb2 import Path, Update
except ImportError as exc:  # pragma: no cover - guards against pygnmi restructuring
    raise ImportError(
        "robotframework-gnmi could not import the gNMI protobuf bindings from pygnmi "
        "(pygnmi.spec.v080.gnmi_pb2). The installed pygnmi version is not supported. "
        "See GNMI/_prefix_workaround.py."
    ) from exc

# Request construction happens inside the worker thread spawned by
# GNMI._run_with_timeout, so the toggle has to be thread-local and must be set
# from within that thread -- see with_prefix_mode().
_state = threading.local()


def keep_prefix_enabled() -> bool:
    """True if the workaround is currently suppressed for this thread."""
    return getattr(_state, "keep_prefix", False)


def with_prefix_mode(func: Callable[..., Any], keep_prefix: bool) -> Callable[..., Any]:
    """Wrap ``func`` so it runs with the given prefix mode in its own thread."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        previous = getattr(_state, "keep_prefix", False)
        _state.keep_prefix = bool(keep_prefix)
        try:
            return func(*args, **kwargs)
        finally:
            _state.keep_prefix = previous

    return wrapper


def _is_empty(path: "Path") -> bool:
    """True if ``path`` carries no origin, no target and no elements."""
    return not path.origin and not path.elem and not path.target


def merge_prefix_into_path(prefix: "Path", path: "Path") -> "Path":
    """Return a single absolute Path equivalent to ``prefix`` + ``path``.

    The prefix elements are prepended to the path elements. ``origin`` and
    ``target`` are taken from the path when set, otherwise inherited from the
    prefix -- matching the gNMI rule that a prefix applies to all paths in the
    message.
    """
    merged = Path()
    origin = path.origin or prefix.origin
    if origin:
        merged.origin = origin
    target = path.target or prefix.target
    if target:
        merged.target = target
    merged.elem.extend(prefix.elem)
    merged.elem.extend(path.elem)
    return merged


def _merge_into_update(prefix: "Path", update: "Update") -> "Update":
    """Return a copy of ``update`` whose path has ``prefix`` folded in."""
    merged = Update()
    merged.CopyFrom(update)
    merged.path.CopyFrom(merge_prefix_into_path(prefix, update.path))
    return merged


def _rewrite(kwargs: dict, path_fields: tuple, update_fields: tuple) -> dict:
    """Drop the ``prefix`` kwarg, folding it into the relevant path fields."""
    prefix = kwargs.get("prefix")
    if prefix is None or keep_prefix_enabled():
        return kwargs

    kwargs.pop("prefix")
    if _is_empty(prefix):
        # Nothing to fold in; simply omitting the empty field is enough.
        return kwargs

    for field in path_fields:
        if kwargs.get(field):
            kwargs[field] = [merge_prefix_into_path(prefix, p) for p in kwargs[field]]
    for field in update_fields:
        if kwargs.get(field):
            kwargs[field] = [_merge_into_update(prefix, u) for u in kwargs[field]]
    return kwargs


class _RequestProxy:
    """Callable stand-in for a protobuf request class.

    Rewrites the constructor keyword arguments, while forwarding every other
    attribute access to the real class. The forwarding matters: pygnmi reaches
    for class attributes such as ``GetRequest.DataType.Value("ALL")``, so a bare
    function is not a sufficient replacement.
    """

    def __init__(self, wrapped: Any, path_fields: tuple, update_fields: tuple) -> None:
        self._wrapped = wrapped
        self._path_fields = path_fields
        self._update_fields = update_fields

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._wrapped(*args, **_rewrite(kwargs, self._path_fields, self._update_fields))

    def __getattr__(self, item: str) -> Any:
        return getattr(self._wrapped, item)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<prefix-workaround proxy for {self._wrapped!r}>"


def apply() -> bool:
    """Install the request-construction wrappers. Idempotent."""
    for name in ("GetRequest", "SetRequest"):
        if not hasattr(pygnmi.client, name):
            raise ImportError(
                f"robotframework-gnmi expected pygnmi.client.{name} to exist in order to "
                "apply the gNMI prefix-origin workaround, but it does not. The installed "
                "pygnmi version is not supported. See GNMI/_prefix_workaround.py."
            )

    if getattr(pygnmi.client, "_rfgnmi_prefix_workaround_applied", False):
        return True

    pygnmi.client.GetRequest = _RequestProxy(pygnmi.client.GetRequest, ("path",), ())
    pygnmi.client.SetRequest = _RequestProxy(pygnmi.client.SetRequest, ("delete",), ("update", "replace"))
    pygnmi.client._rfgnmi_prefix_workaround_applied = True
    return True
