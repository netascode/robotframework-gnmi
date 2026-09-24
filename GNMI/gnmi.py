import threading
from typing import Any, Callable, Optional

from pygnmi.client import gNMIclient, gNMIException
from robot.api import logger
from robot.api.deco import keyword

from . import _prefix_workaround

# Some gNMI servers (observed on IOS-XR 25.4.2) reject any request whose
# ``prefix`` field is present with an origin differing from the path origin.
# pygnmi always emits that field, so every origin-bearing request fails there.
# Installing the wrappers is inert until a caller opts in via
# merge_prefix_into_path. See GNMI/_prefix_workaround.py for the full analysis.
_prefix_workaround.apply()


class GNMI:
    ROBOT_LIBRARY_SCOPE = "GLOBAL"

    def __init__(self) -> None:
        self.sessions: dict[str, gNMIclient] = {}
        self.operation_timeout: Optional[int] = None  # Global timeout for all operations
        self.merge_prefix_into_path: dict[str, bool] = {}  # Per-session prefix-workaround opt-in

    @keyword("GNMI connect session")
    def connect_session(
        self,
        session: str,
        timeout: Optional[int] = None,
        operation_timeout: Optional[int] = None,
        merge_prefix_into_path: bool = False,
        **kwargs: Any,
    ) -> None:
        """
        Establish a gNMI session.

        The timeout argument (optional) is the connection timeout in seconds.
        The operation_timeout argument (optional) sets a default timeout applied
        to subsequent get/set operations on this session.

        merge_prefix_into_path (optional, default False) enables a
        compatibility workaround. Some gNMI servers (observed on IOS-XR 25.4.2)
        reject any request carrying a ``prefix`` field whose origin differs
        from the path origin, failing with "prefix and path origins do not
        match". When enabled, the request prefix is folded into each path and
        the prefix field is omitted. The rewritten request is equivalent and
        complies with gNMI specification section 2.7.

        Leave it unset (the default) for stock pygnmi behaviour. It can be
        overridden per call on ``GNMI get`` and ``GNMI set``.

        All remaining arguments are passed through to pygnmi's gNMIclient.
        """
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
        self.merge_prefix_into_path[session] = bool(merge_prefix_into_path)

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

    def _execute(
        self,
        func: Callable[..., Any],
        timeout: Optional[int],
        merge_prefix: bool,
        **kwargs: Any,
    ) -> Any:
        """Run a pygnmi operation with the requested prefix-merge mode.

        If the workaround is off and the server rejects the request with the
        origin-mismatch error it exists for, the error is re-raised with a hint
        naming the argument that fixes it -- otherwise the failure is opaque and
        the user has no way to discover the option.
        """
        try:
            return self._run_with_timeout(
                _prefix_workaround.with_merge_mode(func, merge_prefix),
                timeout,
                **kwargs,
            )
        except Exception as e:
            if not merge_prefix and _prefix_workaround.is_origin_mismatch_error(e):
                logger.warn(_prefix_workaround.ORIGIN_MISMATCH_HINT)
                raise gNMIException(f"{e}\n\n{_prefix_workaround.ORIGIN_MISMATCH_HINT}", e) from e
            raise

    @keyword("GNMI get")
    def get(
        self,
        session: str,
        prefix: str = "",
        path: list[str] = [],
        datatype: str = "all",
        encoding: str = "json",
        timeout: Optional[int] = None,
        merge_prefix_into_path: Optional[bool] = None,
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

        merge_prefix_into_path (optional) overrides the session's
        prefix-workaround setting for this call only. See ``GNMI connect
        session`` for details. Leave unset to inherit the session default.
        """
        if not (session and session in self.sessions):
            raise ValueError(f"Session {session} is not established, please connect it first")

        # Use per-test timeout, or fall back to global operation timeout
        effective_timeout = timeout if timeout is not None else self.operation_timeout

        if effective_timeout:
            logger.debug(f"Executing GNMI get with {effective_timeout}s timeout")

        effective_merge = (
            merge_prefix_into_path
            if merge_prefix_into_path is not None
            else self.merge_prefix_into_path.get(session, False)
        )

        result = self._execute(
            self.sessions[session].get,
            effective_timeout,
            effective_merge,
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
        merge_prefix_into_path: Optional[bool] = None,
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

        merge_prefix_into_path (optional) overrides the session's
        prefix-workaround setting for this call only. See ``GNMI connect
        session`` for details. Leave unset to inherit the session default.
        """

        if not (session and session in self.sessions):
            raise ValueError(f"Session {session} is not established, please connect it first")

        # Use per-test timeout, or fall back to global operation timeout
        effective_timeout = timeout if timeout is not None else self.operation_timeout

        if effective_timeout:
            logger.debug(f"Executing GNMI set with {effective_timeout}s timeout")

        effective_merge = (
            merge_prefix_into_path
            if merge_prefix_into_path is not None
            else self.merge_prefix_into_path.get(session, False)
        )

        result = self._execute(
            self.sessions[session].set,
            effective_timeout,
            effective_merge,
            delete=delete,
            replace=replace,
            update=update,
            encoding=encoding,
        )

        logger.info(f"set() call returned: {result}")

        if result is None:
            raise Exception("Error executing set operation, please check logs for detail")

        return result
