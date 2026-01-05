import pytest
import pickle
from types import MappingProxyType
from common.python.starlarkish.core.struct import struct, Struct

def test_basic_creation_and_access():
    """Tests basic struct creation and attribute access."""
    s = struct(a=1, b="hello")
    assert s.a == 1
    assert s.b == "hello"

def test_access_nonexistent_attribute():
    """Tests that accessing a non-existent attribute raises AttributeError."""
    s = struct(a=1)
    with pytest.raises(AttributeError):
        _ = s.b

def test_immutability():
    """Tests that structs are immutable."""
    s = struct(a=1)
    with pytest.raises(AttributeError, match="Struct is immutable"):
        s.a = 2
    with pytest.raises(AttributeError, match="Struct is immutable"):
        s.b = 2

def test_item_assignment_immutability():
    """Tests that structs do not support item assignment."""
    s = struct(a=1)
    with pytest.raises(TypeError):
        s['a'] = 2
    with pytest.raises(TypeError):
        s['b'] = 2

def test_to_dict():
    """Tests conversion of a simple struct to a dict."""
    s = struct(a=1, b="hello")
    assert s.to_dict() == {"a": 1, "b": "hello"}

def test_to_dict_nested():
    """Tests conversion of a nested struct to a dict."""
    s = struct(a=1, b=struct(c=2, d=3))
    expected = {"a": 1, "b": {"c": 2, "d": 3}}
    assert s.to_dict() == expected

def test_to_dict_with_lists_and_tuples():
    """Tests conversion of a struct with lists and tuples of structs."""
    s = struct(a=[1, struct(b=2)], c=(struct(d=4),))
    # Note: to_dict converts tuples to lists in the output.
    expected = {"a": [1, {"b": 2}], "c": [{"d": 4}]}
    result = s.to_dict()
    assert result == expected

def test_as_mapping():
    """Tests the read-only mapping view of a struct."""
    s = struct(a=1)
    m = s.as_mapping()
    assert isinstance(m, MappingProxyType)
    assert m['a'] == 1

def test_repr():
    """Tests the string representation of a struct."""
    s = struct(a=1, b="hello")
    # Test for presence of items, allowing for different key order in older Pythons.
    r = repr(s)
    assert r.startswith("struct(")
    assert r.endswith(")")
    assert "a=1" in r
    assert "b='hello'" in r
    
    s_empty = struct()
    assert repr(s_empty) == "struct()"

def test_pickle():
    """Tests that structs can be pickled and unpickled correctly."""
    s = struct(a=1, b=struct(c=[1,2,3]), d={'e': 4})
    pickled = pickle.dumps(s)
    unpickled = pickle.loads(pickled)

    assert unpickled.a == s.a
    assert isinstance(unpickled.b, Struct)
    assert unpickled.b.c == s.b.c
    # The factory wraps dicts, so we check that 'd' is a struct.
    assert isinstance(unpickled.d, Struct)
    assert unpickled.d.e == 4
    assert s.to_dict() == unpickled.to_dict()

def test_struct_factory_nested_dict():
    """Tests the struct factory's conversion of nested dicts."""
    s = struct(a=1, b={'c': 2})
    assert isinstance(s, Struct)
    assert s.a == 1
    assert isinstance(s.b, Struct)
    assert s.b.c == 2

def test_struct_factory_nested_list_of_dicts():
    """Tests the struct factory with a list of dicts."""
    s = struct(a=[{'b': 1}, {'c': 2}])
    assert isinstance(s.a, list)
    assert isinstance(s.a[0], Struct)
    assert s.a[0].b == 1
    assert isinstance(s.a[1], Struct)
    assert s.a[1].c == 2

def test_struct_factory_nested_tuple_of_dicts():
    """Tests the struct factory with a tuple of dicts."""
    s = struct(a=({'b': 1},))
    # The factory converts tuples to lists.
    assert isinstance(s.a, list) 
    assert isinstance(s.a[0], Struct)
    assert s.a[0].b == 1

if __name__ == "__main__":
    pytest.main([__file__])
