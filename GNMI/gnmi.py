import threading
from typing import Any, Callable, Optional

import grpc
from pygnmi.client import gNMIclient, gNMIException, process_potentially_json_value
from pygnmi.create_gnmi_path import gnmi_path_degenerator, gnmi_path_generator
from pygnmi.spec.v080.gnmi_pb2 import GetRequest
from robot.api import logger
from robot.api.deco import keyword


def _combine_prefix_path(prefix: str, path_entry: str) -> str:
    """Combine a prefix and a path entry into one self-contained path string
    with the origin embedded via colon syntax (e.g. "module:" + "/leaf" ->
    "/module:leaf"), so the request never needs a separate Prefix message.

    Works around a pygnmi limitation: gNMIclient.get() always attaches a
    Prefix message to the request, even when empty, because Prefix is a
    protobuf message-type field (assigning it marks it "present" on the
    wire regardless of content). Some gNMI servers then reject the request
    with "prefix and path origins do not match", since an empty-but-present
    Prefix conflicts with the origin-bearing Path. Seen on IOS-XR versions
    25.X and higher.
    """
    module = prefix.rstrip(":").lstrip("/")
    rest = path_entry.lstrip("/") if path_entry else ""
    return f"/{module}:{rest}" if rest else f"/{module}:"


def _raw_stub_and_metadata(client: gNMIclient) -> tuple[Any, Any]:
    """Return an already-connected gNMIclient's gRPC stub and auth metadata, so a
    GetRequest can be sent directly, bypassing gNMIclient.get() (see
    _combine_prefix_path for why). pygnmi exposes neither publicly -- both are
    name-mangled with no accessor -- so this reaches into private state
    deliberately, centralized here as the one place to update if pygnmi ever
    renames them.
    """
    try:
        return client._gNMIclient__stub, client._gNMIclient__metadata
    except AttributeError as e:
        raise gNMIException(
            "pygnmi's gNMIclient no longer exposes '__stub'/'__metadata' the way this "
            "workaround expects -- it may have been updated. See _raw_stub_and_metadata().",
            e,
        )


def _parse_get_response(gnmi_message_response: Any) -> Optional[dict[str, Any]]:
    """Reimplementation of pygnmi's own GetResponse-to-dict conversion, so the
    output shape stays byte-identical for callers that already rely on it.
    Only the *request* construction differs (see _combine_prefix_path); this
    is unchanged pygnmi behavior, just extracted for reuse."""
    if not gnmi_message_response:
        return None

    response: dict[str, Any] = {}
    if gnmi_message_response.notification:
        response["notification"] = []
        for notification in gnmi_message_response.notification:
            notification_container: dict[str, Any] = {}
            notification_container["timestamp"] = notification.timestamp if notification.timestamp else 0
            notification_container["prefix"] = (
                gnmi_path_degenerator(notification.prefix) if notification.prefix else None
            )
            notification_container["alias"] = notification.alias if notification.alias else None
            notification_container["atomic"] = notification.atomic

            if notification.update:
                notification_container["update"] = []
                for update_msg in notification.update:
                    update_container: dict[str, Any] = {}
                    update_container["path"] = gnmi_path_degenerator(update_msg.path) if update_msg.path else None

                    if update_msg.HasField("val"):
                        if update_msg.val.HasField("json_ietf_val"):
                            update_container["val"] = process_potentially_json_value(update_msg.val.json_ietf_val)
                        elif update_msg.val.HasField("json_val"):
                            update_container["val"] = process_potentially_json_value(update_msg.val.json_val)
                        elif update_msg.val.HasField("string_val"):
                            update_container["val"] = update_msg.val.string_val
                        elif update_msg.val.HasField("int_val"):
                            update_container["val"] = update_msg.val.int_val
                        elif update_msg.val.HasField("uint_val"):
                            update_container["val"] = update_msg.val.uint_val
                        elif update_msg.val.HasField("bool_val"):
                            update_container["val"] = update_msg.val.bool_val
                        elif update_msg.val.HasField("float_val"):
                            update_container["val"] = update_msg.val.float_val
                        elif update_msg.val.HasField("decimal_val"):
                            update_container["val"] = update_msg.val.decimal_val
                        elif update_msg.val.HasField("any_val"):
                            update_container["val"] = update_msg.val.any_val
                        elif update_msg.val.HasField("ascii_val"):
                            update_container["val"] = update_msg.val.ascii_val
                        elif update_msg.val.HasField("proto_bytes"):
                            update_container["val"] = update_msg.val.proto_bytes

                    notification_container["update"].append(update_container)

            response["notification"].append(notification_container)

    return response


class GNMI:
    ROBOT_LIBRARY_SCOPE = "GLOBAL"

    def __init__(self) -> None:
        self.sessions: dict[str, gNMIclient] = {}
        self.operation_timeout: Optional[int] = None  # Global timeout for all operations

    @keyword("GNMI connect session")
    def connect_session(
        self,
        session: str,
        timeout: Optional[int] = None,
        operation_timeout: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        if not session:
            raise ValueError("need to provide a non-empty session parameter")
        if session in self.sessions:
            raise ValueError(f"Session {session} is already connected")

        if "debug" not in kwargs:
            kwargs["debug"] = True

        # Set global operation timeout if provided
        if operation_timeout is not None:
            self.operation_timeout = operation_timeout
            logger.info(f"Setting global operation timeout to {self.operation_timeout} seconds")

        # TODO: for now just pass all kwargs into gNMIclient, reckon we want to
        # expose a few of the kwargs as reqired args, not sure what is required
        logger.debug(
            "Starting new session {} with args {}".format(session, ", ".join(f"{k}={v}" for k, v in kwargs.items()))
        )
        self.sessions[session] = gNMIclient(**kwargs)
        self.sessions[session].connect(timeout=timeout)

    def _run_with_timeout(
        self,
        func: Callable[..., Any],
        timeout: Optional[int],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Execute a function with a timeout using threading.

        This allows us to add timeout enforcement to pygnmi operations without
        modifying the pygnmi library itself. The function is executed in a
        daemon thread with a timeout. If the operation exceeds the timeout,
        a TimeoutError is raised.

        Args:
            func: The function to execute
            timeout: Timeout in seconds (int or None). If None, no timeout is applied.
            *args: Positional arguments to pass to func
            **kwargs: Keyword arguments to pass to func

        Returns:
            The return value from func

        Raises:
            TimeoutError: If the operation exceeds the timeout
            Exception: Any exception raised by func is re-raised
        """
        if timeout is None:
            # No timeout, call directly
            return func(*args, **kwargs)

        result = None
        exception = None

        def worker():
            nonlocal result, exception
            try:
                result = func(*args, **kwargs)
            except Exception as e:
                exception = e

        thread = threading.Thread(target=worker)
        thread.daemon = True
        thread.start()
        thread.join(timeout)

        if thread.is_alive():
            # Timeout occurred
            raise TimeoutError(f"Operation timed out after {timeout} seconds")

        if exception:
            raise exception

        return result

    @keyword("GNMI get")
    def get(
        self,
        session: str,
        prefix: str = "",
        path: list[str] = [],
        datatype: str = "all",
        encoding: str = "json",
        timeout: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Collecting the information about the resources from defined paths.

        Path is provided as a list in the following format:
          path = ['yang-module:container/container[key=value]', 'yang-module:container/container[key=value]', ..]
        Available path formats:
          - yang-module:container/container[key=value]
          - /yang-module:container/container[key=value]
          - /yang-module:/container/container[key=value]
          - /container/container[key=value]
          - /
        The datatype argument may have the following values per gNMI specification:
          - all
          - config
          - state
          - operational
        The encoding argument may have the following values per gNMI specification:
          - json
          - bytes
          - proto
          - ascii
          - json_ietf
        The timeout argument (optional) specifies operation timeout in seconds.
        If not provided, uses the global operation_timeout set during connection.
        """
        if not (session and session in self.sessions):
            raise ValueError(f"Session {session} is not established, please connect it first")

        path = path or []
        client = self.sessions[session]

        # Use per-test timeout, or fall back to global operation timeout
        effective_timeout = timeout if timeout is not None else self.operation_timeout

        if effective_timeout:
            logger.debug(f"Executing GNMI get with {effective_timeout}s timeout")

        if prefix:
            # Non-empty prefix only -- see _combine_prefix_path for why this bypasses
            # gNMIclient.get(). Empty-prefix calls fall through to the else branch below,
            # completely unaffected.
            combined = [_combine_prefix_path(prefix, p) for p in (path or [""])]
            try:
                path_objs = [gnmi_path_generator(p) for p in combined]
            except Exception as e:
                logger.error("Conversion of gNMI paths to the Protobuf format failed")
                raise gNMIException("Conversion of gNMI paths to the Protobuf format failed", e)

            try:
                pb_datatype = GetRequest.DataType.Value(datatype.upper())
            except ValueError:
                logger.error(
                    f'The GetRequest data type "{datatype}" is not within the defined range. '
                    "Using default type 'all'."
                )
                pb_datatype = GetRequest.DataType.Value("ALL")
            pb_encoding = client.convert_encoding(encoding)

            stub, metadata = _raw_stub_and_metadata(client)
            request = GetRequest(path=path_objs, type=pb_datatype, encoding=pb_encoding)

            def _do_get() -> Optional[dict[str, Any]]:
                try:
                    raw_response = stub.Get(request, metadata=metadata)
                    return _parse_get_response(raw_response)
                except grpc.RpcError as err:
                    # Public base class -- catches the full RPC-error hierarchy, not just
                    # pygnmi's own private grpc._channel._InactiveRpcError subclass.
                    details = err.details() if hasattr(err, "details") else str(err)
                    logger.critical(f"GRPC ERROR Host: {session}, Error: {details}")
                    raise gNMIException(f"GRPC ERROR Host: {session}, Error: {details}", err)

            result = self._run_with_timeout(_do_get, effective_timeout)
        else:
            result = self._run_with_timeout(
                client.get,
                effective_timeout,
                prefix=prefix,
                path=path,
                datatype=datatype,
                encoding=encoding,
            )

        logger.info(f"get() call returned: {result}")

        if result is None:
            raise Exception("Error retrieving data, please check logs for detail")

        return result

    @keyword("GNMI set")
    def set_(
        self,
        session: str,
        delete: Optional[object] = None,
        replace: Optional[object] = None,
        update: Optional[object] = None,
        encoding: str = "json",
        timeout: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Changing the configuration on the destination network elements.
        Could provide a single attribute or multiple attributes.
        delete:
          - list of paths with the resources to delete. The format is the same as for get() request
        replace:
          - list of tuples where the first entry path provided as a string, and the second entry
            is a dictionary with the configuration to be configured
        replace:
          - list of tuples where the first entry path provided as a string, and the second entry
            is a dictionary with the configuration to be configured
        The encoding argument may have the following values per gNMI specification:
          - json
          - bytes
          - proto
          - ascii
          - json_ietf
        The timeout argument (optional) specifies operation timeout in seconds.
        If not provided, uses the global operation_timeout set during connection.
        """

        if not (session and session in self.sessions):
            raise ValueError(f"Session {session} is not established, please connect it first")

        # Use per-test timeout, or fall back to global operation timeout
        effective_timeout = timeout if timeout is not None else self.operation_timeout

        if effective_timeout:
            logger.debug(f"Executing GNMI set with {effective_timeout}s timeout")

        result = self._run_with_timeout(
            self.sessions[session].set,
            effective_timeout,
            delete=delete,
            replace=replace,
            update=update,
            encoding=encoding,
        )

        logger.info(f"set() call returned: {result}")

        if result is None:
            raise Exception("Error executing set operation, please check logs for detail")

        return result
