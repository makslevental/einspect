"""Test moving of views."""
import pytest

from einspect import view
from einspect.errors import UnsafeError


def test_move_op(new_int: int):
    v = view(new_int)
    v <<= 5
    assert new_int == 5
    assert id(new_int) != id(5)


def test_move_from(new_int: int):
    v = view(new_int)
    v.move_from(view(5))
    assert new_int == 5
    assert id(new_int) != id(5)


def test_move_unsafe_size():
    """Unsafe move due to size."""
    v = view(5)
    with pytest.raises(UnsafeError, match="out of bounds"):
        v <<= 2**90


def test_move_unsafe_gc_types():
    """Unsafe move due to non-gc type into gc type."""
    ls = [1, 2, 3]
    v = view(ls)
    with pytest.raises(UnsafeError, match="gc type"):
        v <<= 10


def test_move_unsafe_instance_dict():
    """Unsafe move due to instance dict."""

    class Num(int):
        pass

    v = view(900)
    with pytest.raises(UnsafeError, match="instance dict"):
        v <<= Num()
