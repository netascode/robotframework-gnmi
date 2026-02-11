import threading
from typing import Any, Callable, Optional

from pygnmi.client import gNMIclient
from robot.api import logger
from robot.api.deco import keyword


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

        # Use per-test timeout, or fall back to global operation timeout
        effective_timeout = timeout if timeout is not None else self.operation_timeout

        if effective_timeout:
            logger.debug(f"Executing GNMI get with {effective_timeout}s timeout")

        result = self._run_with_timeout(
            self.sessions[session].get,
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
