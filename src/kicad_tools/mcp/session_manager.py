"""Session manager for stateful MCP operations.

Provides thread-safe management of multiple concurrent PlacementSession instances,
enabling multi-step placement refinement workflows for AI agents.

Example:
    >>> from kicad_tools.mcp.session_manager import SessionManager
    >>>
    >>> manager = SessionManager(timeout_minutes=30)
    >>>
    >>> # Create a session
    >>> info = manager.create("board.kicad_pcb")
    >>> print(f"Session ID: {info.id}")
    >>>
    >>> # Work with the session
    >>> session = manager.get(info.id)
    >>> result = session.query_move("C1", 45.0, 32.0)
    >>>
    >>> # Commit or rollback
    >>> manager.commit(info.id, "optimized.kicad_pcb")
    >>>
    >>> # Cleanup expired sessions
    >>> removed = manager.cleanup_expired()
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import TYPE_CHECKING

from kicad_tools.mcp.errors import SessionNotFoundError, SessionOperationError
from kicad_tools.mcp.types import SessionInfo
from kicad_tools.optim.session import PlacementSession
from kicad_tools.schema.pcb import PCB

if TYPE_CHECKING:
    pass

__all__ = ["SessionManager"]


class SessionManager:
    """
    Thread-safe manager for multiple concurrent PlacementSession instances.

    Manages session lifecycle including creation, access, commit, rollback,
    and automatic expiration of idle sessions.

    Attributes:
        timeout_seconds: Session timeout in seconds. Sessions not accessed
            within this time will be removed during cleanup.

    Example:
        >>> manager = SessionManager(timeout_minutes=30)
        >>> info = manager.create("/path/to/board.kicad_pcb")
        >>> session = manager.get(info.id)
        >>> # ... use session ...
        >>> manager.commit(info.id, "/path/to/output.kicad_pcb")
    """

    def __init__(self, timeout_minutes: int = 30) -> None:
        """
        Initialize the session manager.

        Args:
            timeout_minutes: Session timeout in minutes. Default is 30 minutes.
                Sessions not accessed within this time will be expired during
                cleanup operations.
        """
        self._sessions: dict[str, PlacementSession] = {}
        self._metadata: dict[str, SessionInfo] = {}
        self._lock = Lock()
        self.timeout_seconds = timeout_minutes * 60

    def _generate_session_id(self) -> str:
        """Generate a unique session ID (8-character UUID prefix)."""
        return str(uuid.uuid4())[:8]

    def _now_iso(self) -> str:
        """Get current time as ISO 8601 string."""
        return datetime.now(timezone.utc).isoformat()

    def create(
        self,
        pcb_path: str,
        fixed_refs: list[str] | None = None,
    ) -> SessionInfo:
        """
        Create a new placement session.

        Loads the PCB file and initializes a PlacementSession for interactive
        placement refinement.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            fixed_refs: Optional list of component references that should not
                be moved during optimization.

        Returns:
            SessionInfo with session metadata including the unique session ID.

        Raises:
            FileNotFoundError: If the PCB file does not exist.
            ValueError: If the PCB file is invalid.
        """
        session_id = self._generate_session_id()
        pcb = PCB.load(pcb_path)
        session = PlacementSession(pcb, fixed_refs=fixed_refs)

        now = self._now_iso()
        info = SessionInfo(
            id=session_id,
            pcb_path=pcb_path,
            created_at=now,
            last_accessed=now,
            pending_moves=0,
            components=len(session._optimizer.components),
            current_score=session._compute_score(),
        )

        with self._lock:
            self._sessions[session_id] = session
            self._metadata[session_id] = info

        return info

    def get(self, session_id: str) -> PlacementSession:
        """
        Get a session by ID, updating last_accessed timestamp.

        Args:
            session_id: The session ID returned from create().

        Returns:
            The PlacementSession instance.

        Raises:
            SessionNotFoundError: If the session ID does not exist.
        """
        with self._lock:
            if session_id not in self._sessions:
                raise SessionNotFoundError(session_id)

            # Update last_accessed time
            info = self._metadata[session_id]
            session = self._sessions[session_id]

            # Update metadata with current state
            self._metadata[session_id] = SessionInfo(
                id=info.id,
                pcb_path=info.pcb_path,
                created_at=info.created_at,
                last_accessed=self._now_iso(),
                pending_moves=len(session.pending_moves),
                components=info.components,
                current_score=session._compute_score(),
            )

            return session

    def get_info(self, session_id: str) -> SessionInfo:
        """
        Get session metadata without updating last_accessed.

        Args:
            session_id: The session ID.

        Returns:
            SessionInfo with current metadata.

        Raises:
            SessionNotFoundError: If the session ID does not exist.
        """
        with self._lock:
            if session_id not in self._metadata:
                raise SessionNotFoundError(session_id)

            info = self._metadata[session_id]
            session = self._sessions[session_id]

            # Return updated info without modifying last_accessed
            return SessionInfo(
                id=info.id,
                pcb_path=info.pcb_path,
                created_at=info.created_at,
                last_accessed=info.last_accessed,
                pending_moves=len(session.pending_moves),
                components=info.components,
                current_score=session._compute_score(),
            )

    def list_sessions(self) -> list[SessionInfo]:
        """
        List all active sessions with their metadata.

        Returns:
            List of SessionInfo for all active sessions.
        """
        with self._lock:
            result = []
            for session_id, info in self._metadata.items():
                session = self._sessions[session_id]
                result.append(
                    SessionInfo(
                        id=info.id,
                        pcb_path=info.pcb_path,
                        created_at=info.created_at,
                        last_accessed=info.last_accessed,
                        pending_moves=len(session.pending_moves),
                        components=info.components,
                        current_score=session._compute_score(),
                    )
                )
            return result

    def commit(self, session_id: str, output_path: str | None = None) -> str:
        """
        Commit session changes and save to file.

        Applies all pending moves to the PCB and saves the result.
        The session remains active after commit.

        Args:
            session_id: The session ID.
            output_path: Path to save the modified PCB. If None, overwrites
                the original file.

        Returns:
            Path where the PCB was saved.

        Raises:
            SessionNotFoundError: If the session ID does not exist.
            SessionOperationError: If the save operation fails.
        """
        with self._lock:
            if session_id not in self._sessions:
                raise SessionNotFoundError(session_id)

            session = self._sessions[session_id]
            info = self._metadata[session_id]

            # Commit changes to the PCB object
            pcb = session.commit()

            # Determine output path
            save_path = output_path or info.pcb_path

            try:
                pcb.save(save_path)
            except Exception as e:
                raise SessionOperationError(session_id, "commit", f"Failed to save PCB: {e}") from e

            # Update metadata
            self._metadata[session_id] = SessionInfo(
                id=info.id,
                pcb_path=save_path,
                created_at=info.created_at,
                last_accessed=self._now_iso(),
                pending_moves=0,
                components=info.components,
                current_score=session._compute_score(),
            )

            return save_path

    def rollback(self, session_id: str) -> SessionInfo:
        """
        Rollback session to initial state, discarding all pending moves.

        Args:
            session_id: The session ID.

        Returns:
            Updated SessionInfo after rollback.

        Raises:
            SessionNotFoundError: If the session ID does not exist.
        """
        with self._lock:
            if session_id not in self._sessions:
                raise SessionNotFoundError(session_id)

            session = self._sessions[session_id]
            info = self._metadata[session_id]

            # Rollback the session
            session.rollback()

            # Update metadata
            new_info = SessionInfo(
                id=info.id,
                pcb_path=info.pcb_path,
                created_at=info.created_at,
                last_accessed=self._now_iso(),
                pending_moves=0,
                components=info.components,
                current_score=session._compute_score(),
            )
            self._metadata[session_id] = new_info

            return new_info

    def close(self, session_id: str) -> bool:
        """
        Close and remove a session.

        Args:
            session_id: The session ID.

        Returns:
            True if session was closed, False if it didn't exist.
        """
        with self._lock:
            if session_id not in self._sessions:
                return False

            del self._sessions[session_id]
            del self._metadata[session_id]
            return True

    def cleanup_expired(self) -> int:
        """
        Remove sessions that have exceeded the timeout.

        Sessions that haven't been accessed within timeout_seconds
        will be removed.

        Returns:
            Number of sessions removed.
        """
        now = datetime.now(timezone.utc)
        expired: list[str] = []

        with self._lock:
            for session_id, info in self._metadata.items():
                last_accessed = datetime.fromisoformat(info.last_accessed)
                elapsed = (now - last_accessed).total_seconds()
                if elapsed > self.timeout_seconds:
                    expired.append(session_id)

            for session_id in expired:
                del self._sessions[session_id]
                del self._metadata[session_id]

        return len(expired)

    def __len__(self) -> int:
        """Return the number of active sessions."""
        with self._lock:
            return len(self._sessions)

    def __contains__(self, session_id: str) -> bool:
        """Check if a session exists."""
        with self._lock:
            return session_id in self._sessions
