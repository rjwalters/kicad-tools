"""Base query classes for fluent filtering.

Provides Django-ORM-style query interface for filtering collections
of KiCad elements like symbols and footprints.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

if TYPE_CHECKING:
    from typing_extensions import Self

T = TypeVar("T")


class BaseQuery(Generic[T]):
    """Base query class with filter logic.

    Supports Django-style field lookups:
    - value="100nF" - exact match
    - value__contains="100" - substring match
    - value__startswith="R" - prefix match
    - value__endswith="nF" - suffix match
    - value__in=["100nF", "10nF"] - membership
    - value__regex=r"\\d+nF" - regex match
    - value__gt=10 - greater than
    - value__lt=100 - less than
    - value__gte=10 - greater than or equal
    - value__lte=100 - less than or equal
    - value__icontains="abc" - case-insensitive contains
    - value__iexact="ABC" - case-insensitive exact match

    Example:
        query = SymbolQuery(symbols)
        caps = query.filter(reference__startswith="C").all()
        r1 = query.filter(reference="R1").first()
    """

    def __init__(self, items: list[T]):
        """Initialize query with items to filter.

        Args:
            items: List of items to query
        """
        self._items = items
        self._filters: list[Callable[[T], bool]] = []

    def _make_filter(self, attr: str, value: Any) -> Callable[[T], bool]:
        """Create a filter function for an attribute lookup.

        Args:
            attr: Attribute name with optional lookup suffix (e.g., "value__contains")
            value: Value to compare against

        Returns:
            Filter function that takes an item and returns bool
        """
        if "__" in attr:
            field, op = attr.rsplit("__", 1)
        else:
            field, op = attr, "exact"

        def check(item: T) -> bool:
            # Handle nested attribute access (e.g., "position.x")
            item_value = item
            for part in field.split("."):
                if hasattr(item_value, part):
                    item_value = getattr(item_value, part)
                elif isinstance(item_value, dict) and part in item_value:
                    item_value = item_value[part]
                else:
                    return False

            # Apply the operator
            if item_value is None:
                return op == "isnull" and value is True

            if op == "exact":
                return bool(item_value == value)
            elif op == "iexact":
                return str(item_value).lower() == str(value).lower()
            elif op == "contains":
                return str(value) in str(item_value)
            elif op == "icontains":
                return str(value).lower() in str(item_value).lower()
            elif op == "startswith":
                return str(item_value).startswith(str(value))
            elif op == "istartswith":
                return str(item_value).lower().startswith(str(value).lower())
            elif op == "endswith":
                return str(item_value).endswith(str(value))
            elif op == "iendswith":
                return str(item_value).lower().endswith(str(value).lower())
            elif op == "in":
                return bool(item_value in value)
            elif op == "regex":
                return bool(re.search(value, str(item_value)))
            elif op == "iregex":
                return bool(re.search(value, str(item_value), re.IGNORECASE))
            elif op == "gt":
                return bool(item_value > value)
            elif op == "gte":
                return bool(item_value >= value)
            elif op == "lt":
                return bool(item_value < value)
            elif op == "lte":
                return bool(item_value <= value)
            elif op == "isnull":
                return bool((item_value is None) == value)
            else:
                # Unknown operator, fall back to exact match
                return bool(item_value == value)

        return check

    def filter(self, **kwargs: Any) -> BaseQuery[T]:
        """Filter items by attribute values.

        Creates a new query with additional filters applied.
        Filters are combined with AND logic.

        Args:
            **kwargs: Field lookups (e.g., value="100nF", reference__startswith="C")

        Returns:
            New query with filters applied

        Example:
            query.filter(value="100nF", footprint__contains="0402")
        """
        # Create a new query to allow chaining without mutation
        new_query = self.__class__(self._items)
        new_query._filters = self._filters.copy()

        for attr, value in kwargs.items():
            new_query._filters.append(self._make_filter(attr, value))

        return new_query

    def exclude(self, **kwargs: Any) -> BaseQuery[T]:
        """Exclude items matching criteria.

        Opposite of filter() - removes items that match.

        Args:
            **kwargs: Field lookups to exclude

        Returns:
            New query with exclusions applied

        Example:
            query.exclude(lib_id__startswith="power:")
        """
        new_query = self.__class__(self._items)
        new_query._filters = self._filters.copy()

        for attr, value in kwargs.items():
            include_filter = self._make_filter(attr, value)

            # Negate the filter - explicit function to help type inference
            def make_negated(f: Callable[[T], bool]) -> Callable[[T], bool]:
                return lambda item: not f(item)

            new_query._filters.append(make_negated(include_filter))

        return new_query

    def all(self) -> list[T]:
        """Execute query and return all matching items.

        Returns:
            List of all items matching all filters
        """
        result = self._items
        for f in self._filters:
            result = [item for item in result if f(item)]
        return result

    def first(self) -> T | None:
        """Return first matching item or None.

        More efficient than all()[0] as it stops at first match.

        Returns:
            First matching item, or None if no matches
        """
        for item in self._items:
            if all(f(item) for f in self._filters):
                return item
        return None

    def last(self) -> T | None:
        """Return last matching item or None.

        Returns:
            Last matching item, or None if no matches
        """
        result = self.all()
        return result[-1] if result else None

    def count(self) -> int:
        """Count matching items.

        Returns:
            Number of items matching all filters
        """
        return len(self.all())

    def exists(self) -> bool:
        """Check if any items match.

        More efficient than count() > 0 as it stops at first match.

        Returns:
            True if at least one item matches
        """
        return self.first() is not None

    def values(self, *fields: str) -> list[dict[str, Any]]:
        """Return list of dicts with specified fields.

        Args:
            *fields: Field names to include

        Returns:
            List of dicts with requested fields
        """
        result: list[dict[str, Any]] = []
        for item in self.all():
            d: dict[str, Any] = {}
            for field in fields:
                if hasattr(item, field):
                    d[field] = getattr(item, field)
            result.append(d)
        return result

    def values_list(self, *fields: str, flat: bool = False) -> list[Any]:
        """Return list of tuples with specified fields.

        Args:
            *fields: Field names to include
            flat: If True and single field, return flat list

        Returns:
            List of tuples (or flat list if flat=True)
        """
        result: list[Any] = []
        for item in self.all():
            values: tuple[Any, ...] = tuple(getattr(item, field, None) for field in fields)
            if flat and len(fields) == 1:
                result.append(values[0])
            else:
                result.append(values)
        return result

    def order_by(self, *fields: str) -> BaseQuery[T]:
        """Order results by specified fields.

        Prefix field with '-' for descending order.

        Args:
            *fields: Field names to order by (prefix with '-' for descending)

        Returns:
            New query with ordering applied
        """
        new_query = self.__class__(self._items)
        new_query._filters = self._filters.copy()

        def sort_key(item: T) -> tuple[Any, ...]:
            keys: list[Any] = []
            for field in fields:
                desc = field.startswith("-")
                if desc:
                    field = field[1:]
                val: Any = getattr(item, field, None)
                # Handle None values
                if val is None:
                    val = "" if desc else "\xff" * 100
                keys.append(val)
            return tuple(keys)

        # Sort the items - we need to execute filters first
        sorted_items = sorted(self.all(), key=sort_key)

        # Check for descending fields
        for field in reversed(fields):
            if field.startswith("-"):
                sorted_items = list(reversed(sorted_items))
                break

        # Replace items with sorted list and clear filters (already applied)
        new_query._items = sorted_items
        new_query._filters = []
        return new_query

    def __iter__(self) -> Iterator[T]:
        """Iterate over matching items."""
        return iter(self.all())

    def __len__(self) -> int:
        """Return count of matching items."""
        return self.count()

    def __bool__(self) -> bool:
        """Return True if any items match."""
        return self.exists()

    def __getitem__(self, index: int) -> T:
        """Get item by index from results."""
        return self.all()[index]


class ComponentQueryMixin(Generic[T]):
    """Mixin providing common query methods for component-like items.

    Provides convenience methods shared between SymbolQuery and FootprintQuery
    for filtering by reference designator, value, and component type.

    This mixin expects the class to have a `filter()` method that returns
    a query of the same type (for chaining).
    """

    def by_reference(self, reference: str) -> T | None:
        """Get item by reference designator.

        Args:
            reference: Reference designator (e.g., "U1", "R1", "C1")

        Returns:
            Item with matching reference, or None

        Example:
            u1 = query.by_reference("U1")
        """
        return self.filter(reference=reference).first()  # type: ignore[attr-defined]

    def by_value(self, value: str) -> Self:
        """Filter by value.

        Args:
            value: Component value (e.g., "10k", "100nF")

        Returns:
            Query filtered to matching value

        Example:
            caps_100nf = query.by_value("100nF").all()
        """
        return cast("Self", self.filter(value=value))  # type: ignore[attr-defined]

    def capacitors(self) -> Self:
        """Filter to capacitors (C* references).

        Returns:
            Query filtered to capacitors
        """
        return cast("Self", self.filter(reference__startswith="C"))  # type: ignore[attr-defined]

    def resistors(self) -> Self:
        """Filter to resistors (R* references).

        Returns:
            Query filtered to resistors
        """
        return cast("Self", self.filter(reference__startswith="R"))  # type: ignore[attr-defined]

    def ics(self) -> Self:
        """Filter to ICs (U* references).

        Returns:
            Query filtered to ICs
        """
        return cast("Self", self.filter(reference__startswith="U"))  # type: ignore[attr-defined]

    def connectors(self) -> Self:
        """Filter to connectors (J* references).

        Returns:
            Query filtered to connectors
        """
        return cast("Self", self.filter(reference__startswith="J"))  # type: ignore[attr-defined]
