"""Session manager for stateful MCP operations.

Manages multiple concurrent PlacementSession instances, providing
session lifecycle management, timeout handling, and thread-safe access.

Example:
    >>> manager = SessionManager(timeout_minutes=30)
    >>> info = manager.create("/path/to/board.kicad_pcb", fixed_refs=["J1", "J2"])
    >>> session = manager.get(info.id)
    >>> result = session.query_move("C1", 45.0, 32.0)
    >>> manager.destroy(info.id)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING

from kicad_tools.optim.session import PlacementSession
from kicad_tools.schema.pcb import PCB

if TYPE_CHECKING:
    pass

__all__ = [
    "SessionManager",
    "SessionInfo",
    "SessionNotFoundError",
    "SessionExpiredError",
]


class SessionNotFoundError(Exception):
    """Raised when a session ID is not found."""

    pass


class SessionExpiredError(Exception):
    """Raised when attempting to access an expired session."""

    pass


@dataclass
class SessionInfo:
    """Metadata about a placement session.

    Attributes:
        id: Unique session identifier (8-character hex string)
        pcb_path: Path to the PCB file
        created_at: When the session was created
        last_accessed: When the session was last accessed
        pending_moves: Number of uncommitted moves
        components: Total number of components
        current_score: Current placement score
        fixed_refs: List of fixed component references
    """

    id: str
    pcb_path: str
    created_at: datetime
    last_accessed: datetime
    pending_moves: int
    components: int
    current_score: float
    fixed_refs: list[str]

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "pcb_path": self.pcb_path,
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "pending_moves": self.pending_moves,
            "components": self.components,
            "current_score": round(self.current_score, 4),
            "fixed_refs": self.fixed_refs,
        }


class SessionManager:
    """
    Manages multiple concurrent PlacementSession instances.

    Provides session lifecycle management including creation, access,
    timeout handling, and cleanup. Thread-safe for concurrent access.

    Example:
        >>> manager = SessionManager(timeout_minutes=30)
        >>> info = manager.create("/path/to/board.kicad_pcb")
        >>> session = manager.get(info.id)
        >>> session.query_move("C1", 45.0, 32.0)
        >>> manager.destroy(info.id)
    """

    def __init__(self, timeout_minutes: int = 30):
        """
        Initialize the session manager.

        Args:
            timeout_minutes: Session timeout in minutes (default 30)
        """
        self._sessions: dict[str, PlacementSession] = {}
        self._metadata: dict[str, SessionInfo] = {}
        self._lock = Lock()
        self._timeout = timedelta(minutes=timeout_minutes)

    def create(
        self,
        pcb_path: str,
        fixed_refs: list[str] | None = None,
    ) -> SessionInfo:
        """
        Create a new placement session.

        Args:
            pcb_path: Path to .kicad_pcb file
            fixed_refs: Optional list of component references that cannot be moved

        Returns:
            SessionInfo with session ID and metadata

        Raises:
            FileNotFoundError: If PCB file doesn't exist
            ParseError: If PCB file cannot be parsed
        """
        session_id = uuid.uuid4().hex[:8]
        pcb = PCB.load(pcb_path)
        session = PlacementSession(pcb, fixed_refs=fixed_refs)

        now = datetime.now()
        fixed = fixed_refs or []

        info = SessionInfo(
            id=session_id,
            pcb_path=pcb_path,
            created_at=now,
            last_accessed=now,
            pending_moves=0,
            components=len(session._optimizer.components),
            current_score=session._compute_score(),
            fixed_refs=fixed,
        )

        with self._lock:
            self._sessions[session_id] = session
            self._metadata[session_id] = info

        return info

    def get(self, session_id: str) -> PlacementSession:
        """
        Get a session by ID, updating last_accessed timestamp.

        Args:
            session_id: Session ID from create()

        Returns:
            PlacementSession instance

        Raises:
            SessionNotFoundError: If session ID is not found
            SessionExpiredError: If session has expired
        """
        with self._lock:
            if session_id not in self._sessions:
                raise SessionNotFoundError(f"Session '{session_id}' not found")

            # Check for expiration
            info = self._metadata[session_id]
            if datetime.now() - info.last_accessed > self._timeout:
                # Clean up expired session
                del self._sessions[session_id]
                del self._metadata[session_id]
                raise SessionExpiredError(
                    f"Session '{session_id}' has expired (timeout: {self._timeout})"
                )

            # Update last accessed time
            info.last_accessed = datetime.now()

            # Update pending moves count
            session = self._sessions[session_id]
            info.pending_moves = len(session.pending_moves)
            info.current_score = session._compute_score()

            return session

    def get_info(self, session_id: str) -> SessionInfo:
        """
        Get session metadata without updating last_accessed.

        Args:
            session_id: Session ID from create()

        Returns:
            SessionInfo with current metadata

        Raises:
            SessionNotFoundError: If session ID is not found
        """
        with self._lock:
            if session_id not in self._metadata:
                raise SessionNotFoundError(f"Session '{session_id}' not found")

            info = self._metadata[session_id]

            # Update dynamic fields from session
            session = self._sessions[session_id]
            info.pending_moves = len(session.pending_moves)
            info.current_score = session._compute_score()

            return info

    def destroy(self, session_id: str) -> bool:
        """
        Destroy a session and free resources.

        Args:
            session_id: Session ID from create()

        Returns:
            True if session was destroyed, False if not found
        """
        with self._lock:
            if session_id not in self._sessions:
                return False

            del self._sessions[session_id]
            del self._metadata[session_id]
            return True

    def cleanup_expired(self) -> int:
        """
        Remove all expired sessions.

        Returns:
            Number of sessions removed
        """
        now = datetime.now()
        expired: list[str] = []

        with self._lock:
            for session_id, info in self._metadata.items():
                if now - info.last_accessed > self._timeout:
                    expired.append(session_id)

            for session_id in expired:
                del self._sessions[session_id]
                del self._metadata[session_id]

        return len(expired)

    def list_sessions(self) -> list[SessionInfo]:
        """
        List all active sessions.

        Returns:
            List of SessionInfo for all active sessions
        """
        with self._lock:
            return list(self._metadata.values())

    @property
    def session_count(self) -> int:
        """Return number of active sessions."""
        with self._lock:
            return len(self._sessions)


# Global session manager instance for MCP tools
_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """Get or create the global session manager instance."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
