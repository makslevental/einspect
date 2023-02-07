from __future__ import annotations

import sys
import weakref
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Any, Callable, Literal, Type, TypeVar, Union, get_args

from typing_extensions import Self

from einspect._typing import is_union
from einspect.compat import Version
from einspect.errors import UnsafeError
from einspect.structs import PyTypeObject
from einspect.structs.include.object_h import TpFlags
from einspect.structs.py_type import TypeNewWrapper
from einspect.structs.slots_map import (
    Slot,
    get_slot,
    tp_as_async,
    tp_as_mapping,
    tp_as_number,
    tp_as_sequence,
)
from einspect.type_orig import add_cache, get_cache, in_cache
from einspect.views.view_base import REF_DEFAULT, VarView

if TYPE_CHECKING:
    if sys.version_info >= (3, 9):
        from types import UnionType
    else:
        UnionType = type(Union)

__all__ = ("TypeView", "impl")

_T = TypeVar("_T")
_Fn = TypeVar("_Fn", bound=Callable)

AllocMode = Literal["mapping", "sequence", "all"]
ALLOC_MODES = frozenset({"mapping", "sequence", "all"})


def get_func_name(func: Callable) -> str:
    """Returns the name of the function."""
    return get_func_base(func).__name__


def get_func_base(func: Callable) -> Callable:
    """Returns the base function of a method or property."""
    if isinstance(func, property):
        return func.fget
    elif isinstance(func, (classmethod, staticmethod)):
        return func.__func__
    return func


def _to_types(
    types_or_unions: Sequence[type | UnionType],
) -> Generator[type, None, None]:
    """Yields types from a Sequence of types or unions."""
    for t in types_or_unions:
        if isinstance(t, type):
            yield t
        elif is_union(t):
            yield from _to_types(get_args(t))
        else:
            raise TypeError(f"cls must be a type or Union, not {t.__class__.__name__}")


def _restore_impl(*types: type, name: str) -> None:
    """
    Finalizer to restore the original `name` attribute on type(s).

    If there is no original attribute, delete the attribute.
    """
    for t in types:
        v = TypeView(t, ref=False)
        # Get the original attribute from cache
        if in_cache(t, name):
            attr = get_cache(t, name)
            # For TypeNewWrapper, use the original slot wrapper
            if isinstance(t, TypeNewWrapper):
                attr = t._orig_slot_fn
            # Set the attribute back using a view
            v[name] = attr
        else:
            # If there is no original attribute, delete the attribute
            with v.as_mutable():
                delattr(t, name)


def _attach_finalizer(types: Sequence[type], func: Callable) -> None:
    """Attaches a finalizer to the function to remove the implemented method on types."""
    # Use the base function (we can't set attributes on properties)
    func = get_func_base(func)
    name = func.__name__
    # Don't finalize types that are already registered
    if hasattr(func, "_impl_types"):
        types = [t for t in types if t not in func._impl_types]
        # Update list
        func._impl_types.extend(types)
    else:
        func._impl_types = list(types)

    func._impl_finalize = weakref.finalize(func, _restore_impl, *types, name=name)


def impl(
    *cls: Type[_T] | UnionType,
    alloc: AllocMode | None = None,
    detach: bool = False,
) -> Callable[[_Fn], _Fn]:
    # noinspection PyShadowingNames, PyCallingNonCallable
    """
    Decorator for implementing methods on types.

    Supports methods decorated with property, classmethod, or staticmethod.

    Args:
        cls: The type(s) or Union(s) to implement the method on.
        alloc: The PyMethod allocation mode. Default of None will automatically allocate
            PyMethod structs as needed. If "sequence" or "mapping", will prefer the
            respective PySequenceMethods or PyMappingMethods in cases of ambiguious slot names.
            (e.g. "__getitem__" or "__len__"). If "all", will allocate all PyMethod structs.
        detach: If True, will remove the implemented method from the type when
            the decorated function is garbage collected. This will hold a reference to
            the type(s) for the lifetime of the function. Requires function to support weakrefs.

    Returns:
        The original function after it has been implemented on the types,
        allows chaining of multiple impl decorators.

    Examples:
        >>> @impl(int)
        ... def is_even(self):
        ...     return self % 2 == 0

        >>> @impl(int | float)  # or @impl(int, float)
        ... @classmethod
        ... def try_from(cls, value):
        ...     try:
        ...         return cls(value)
        ...     except ValueError:
        ...         return None
    """
    targets = tuple(_to_types(cls))

    def wrapper(func: _Fn) -> _Fn:
        # detach requires weakrefs
        try:
            weakref.ref(get_func_base(func))
        except TypeError:
            raise TypeError(
                f"detach=True requires function {func!r} to support weakrefs"
            ) from None

        name = get_func_name(func)

        for type_ in targets:
            t_view = TypeView(type_)
            with t_view.alloc_mode(alloc):
                t_view[name] = func

        if detach:
            _attach_finalizer(targets, func)

        return func

    return wrapper


class TypeView(VarView[_T, None, None]):
    _pyobject: PyTypeObject[_T]

    def __init__(self, obj: _T, ref: bool = REF_DEFAULT) -> None:
        """Create a new TypeView."""
        super().__init__(obj, ref)
        self._alloc_mode = None

    @property
    def immutable(self) -> bool:
        """Return True if the type is immutable."""
        if Version.PY_3_10.above():
            return bool(self._pyobject.tp_flags & TpFlags.IMMUTABLETYPE)
        return not bool(self._pyobject.tp_flags & TpFlags.HEAPTYPE)  # pragma: no cover

    @immutable.setter
    def immutable(self, value: bool):
        """Set whether the type is immutable."""
        if Version.PY_3_10.above():
            if value:
                self._pyobject.tp_flags |= TpFlags.IMMUTABLETYPE
            else:
                self._pyobject.tp_flags &= ~TpFlags.IMMUTABLETYPE
        else:  # pragma: no cover
            if value:
                self._pyobject.tp_flags &= ~TpFlags.HEAPTYPE
            else:
                self._pyobject.tp_flags |= TpFlags.HEAPTYPE

    @contextmanager
    def as_mutable(self) -> Generator[Self, None, None]:
        """Context manager to temporarily set the type as mutable."""
        if not self.immutable:
            yield self
            return
        self.immutable = False
        self._pyobject.Modified()
        yield self
        self.immutable = True
        self._pyobject.Modified()

    @contextmanager
    def alloc_mode(self, mode: AllocMode | None):
        """Context manager to temporarily set the type's allocation mode."""
        if mode is None:
            yield self
            return
        mode = mode.casefold() if mode else None
        if mode is not None and mode not in ALLOC_MODES:
            raise ValueError(
                f"Invalid alloc mode: {mode!r}, must be 'mapping', 'sequence', or 'all'"
            )
        orig = self._alloc_mode
        self._alloc_mode = mode
        yield self
        self._alloc_mode = orig

    def _try_alloc(self, slot: Slot, subclasses: bool = True) -> None:
        """Allocate a slot method struct if the pointer is NULL."""
        # Check if there is a ptr class
        if slot.ptr_type is None:
            return
        py_objs = [self._pyobject]
        if subclasses:
            for sub in self._pyobject.GetAttr("__subclasses__")():
                assert isinstance(sub, type)
                py_objs.append(PyTypeObject.from_object(sub))
        for py_obj in py_objs:
            # Check if the slot is a null pointer
            ptr = getattr(py_obj, slot.parts[0])
            if not ptr:
                # Make a new ptr type struct
                new = slot.ptr_type()
                ptr.contents = new

    def alloc_slot(self, *slot: Slot) -> None:
        """
        Allocate slot method structs. Defaults to all the following:

        - tp_as_async (PyAsyncMethods)
        - tp_as_number (PyNumberMethods)
        - tp_as_mapping (PyMappingMethods)
        - tp_as_sequence (PySequenceMethods)
        """
        slots = slot or (tp_as_async, tp_as_number, tp_as_mapping, tp_as_sequence)
        for s in slots:
            self._try_alloc(s)

    def __getitem__(self, key: str) -> Any:
        """Get an attribute from the type object."""
        return self._pyobject.GetAttr(key)

    def __setitem__(self, key: str | tuple[str, ...], value: Any) -> None:
        """
        Set attributes on the type object.

        Multiple string keys can be used to set multiple attributes to the same value.
        """
        keys = (key,) if isinstance(key, str) else key
        # For all alloc mode, allocate now
        if self._alloc_mode == "all":
            self.alloc_slot()
        for name in keys:
            # Cache original implementation
            base = self.base
            if not in_cache(base, name):
                with suppress(AttributeError):
                    attr = getattr(base, name)
                    add_cache(base, name, attr)
            # Check if this is a slots attr (skip all since we already allocated)
            if self._alloc_mode != "all" and (
                slot := get_slot(name, prefer=self._alloc_mode)
            ):
                # Allocate sub-struct if needed
                self._try_alloc(slot)

            with self.as_mutable():
                self._pyobject.setattr_safe(name, value)

    # <-- Begin Managed::Properties (structs::py_type.PyTypeObject) -->

    # <-- End Managed::Properties -->

    def __getattr__(self, item: str):
        # Forward `tp_` attributes from PyTypeObject
        if item.startswith("tp_"):
            return getattr(self._pyobject, item)
        raise AttributeError(item)

    def __setattr__(self, key: str, value: Any) -> None:
        # Forward `tp_` attributes from PyTypeObject
        if key.startswith("tp_") and hasattr(self._pyobject, key):
            if not self._unsafe:
                raise UnsafeError(
                    f"Setting attribute {key} requires an unsafe context."
                )
            setattr(self._pyobject, key, value)
            return
        super().__setattr__(key, value)
