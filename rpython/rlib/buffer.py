"""
Buffer protocol support.
"""
from rpython.rtyper.lltypesystem import lltype, llmemory
from rpython.rtyper.lltypesystem.lloperation import llop
from rpython.rtyper.lltypesystem.rstr import STR
from rpython.rtyper.lltypesystem.rlist import LIST_OF
from rpython.rtyper.annlowlevel import llstr
from rpython.rlib.objectmodel import specialize
from rpython.rlib import jit
from rpython.rlib.rgc import (resizable_list_supporting_raw_ptr,
                              nonmoving_raw_ptr_for_resizable_list,
                              ll_for_resizable_list)
from rpython.rlib.signature import signature
from rpython.rlib import types

class CannotRead(Exception):
    """
    Exception raised by Buffer.typed_read in case it is not possible to
    accomplish the request. This might be because it is not supported by the
    specific type of buffer, or because of alignment issues.
    """

class CannotWrite(Exception):
    """
    Raised by Buffer.typed_write in case it is not possible to accomplish the
    request
    """

class Buffer(object):
    """
    Base class for buffers of bytes.

    Most probably, you do NOT want to use this as a base class, but either
    GCBuffer or RawBuffer, so that you automatically get the proper
    implementation of typed_read and typed_write.
    """
    _attrs_ = ['readonly']
    _immutable_ = True

    def getlength(self):
        """Return the size in bytes."""
        raise NotImplementedError

    def __len__(self):
        res = self.getlength()
        assert res >= 0
        return res

    def as_str(self):
        "Returns an interp-level string with the whole content of the buffer."
        # May be overridden.
        return self.getslice(0, self.getlength(), 1, self.getlength())

    def getitem(self, index):
        "Returns the index'th character in the buffer."
        raise NotImplementedError   # Must be overriden.  No bounds checks.

    def __getitem__(self, i):
        return self.getitem(i)

    def getslice(self, start, stop, step, size):
        # May be overridden.  No bounds checks.
        return ''.join([self.getitem(i) for i in range(start, stop, step)])

    @signature(types.any(), types.int(), types.int(), returns=types.str())
    def __getslice__(self, start, stop):
        return self.getslice(start, stop, 1, stop - start)

    def setitem(self, index, char):
        "Write a character into the buffer."
        raise NotImplementedError   # Must be overriden.  No bounds checks.

    def __setitem__(self, i, char):
        return self.setitem(i, char)

    def setslice(self, start, string):
        # May be overridden.  No bounds checks.
        for i in range(len(string)):
            self.setitem(start + i, string[i])

    @specialize.ll_and_arg(1)
    def typed_read(self, TP, byte_offset):
        """
        Read the value of type TP starting at byte_offset. No bounds checks
        """
        raise CannotRead

    @specialize.ll_and_arg(1)
    def typed_write(self, TP, byte_offset, value):
        """
        Write the value of type TP at byte_offset. No bounds checks
        """
        raise CannotWrite


class RawBuffer(Buffer):
    """
    A buffer which is baked by a raw, non-movable memory area. It implementes
    typed_read and typed_write in terms of get_raw_address(), llop.raw_load,
    llop.raw_store.

    NOTE: this assumes that get_raw_address() is cheap. Do not use this as a
    base class if get_raw_address() is potentially costly, like for example if
    you call rgc.nonmoving_raw_ptr_for_resizable_list
    """
    _immutable_ = True

    @specialize.ll_and_arg(1)
    def typed_read(self, TP, byte_offset):
        """
        Read the value of type TP starting at byte_offset. No bounds checks
        """
        ptr = self.get_raw_address()
        return llop.raw_load(TP, ptr, byte_offset)

    @specialize.ll_and_arg(1)
    def typed_write(self, TP, byte_offset, value):
        """
        Write the value of type TP at byte_offset. No bounds checks
        """
        if self.readonly:
            raise CannotWrite
        ptr = self.get_raw_address()
        value = lltype.cast_primitive(TP, value)
        return llop.raw_store(lltype.Void, ptr, byte_offset, value)


class GCBuffer(Buffer):
    """
    A buffer which is baked by a GC-managed memory area. It implements
    typed_read and typed_write in terms of llop.gc_load_indexed and
    llop.gc_store_indexed.

    Subclasses MUST override the _get_gc_* methods.
    """
    _immutable_ = True
    _attrs_ = ['readonly']

    def _get_gc_data(self):
        raise NotImplementedError

    def _get_gc_data_offset(self):
        """
        Return the offset to use with _get_gc_data() for calling
        llop.gc_{load,store}_indexed.
        """
        raise NotImplementedError

    @specialize.ll_and_arg(1)
    def typed_read(self, TP, byte_offset):
        """
        Read the value of type TP starting at byte_offset. No bounds checks
        """
        lldata = self._get_gc_data()
        base_ofs = self._get_gc_data_offset()
        scale_factor = llmemory.sizeof(lltype.Char)
        return llop.gc_load_indexed(TP, lldata, byte_offset,
                                    scale_factor, base_ofs)

    @specialize.ll_and_arg(1)
    def typed_write(self, TP, byte_offset, value):
        """
        Write the value of type TP at byte_offset. No bounds checks
        """
        if self.readonly:
            raise CannotWrite
        lldata = self._get_gc_data()
        base_ofs = self._get_gc_data_offset()
        scale_factor = llmemory.sizeof(lltype.Char)
        value = lltype.cast_primitive(TP, value)
        return llop.gc_store_indexed(lltype.Void, lldata, byte_offset, value,
                                     scale_factor, base_ofs)


def get_gc_data_for_list_of_chars(data):
    ll_data = ll_for_resizable_list(data)
    ll_items = ll_data.items
    return lltype.cast_opaque_ptr(llmemory.GCREF, ll_items)

def get_gc_data_offset_for_list_of_chars():
    LIST = LIST_OF(lltype.Char)
    return llmemory.itemoffsetof(LIST.items.TO, 0)


class ByteBuffer(GCBuffer):
    _immutable_ = True

    def __init__(self, n):
        self.data = resizable_list_supporting_raw_ptr(['\0'] * n)
        self.readonly = False

    def getlength(self):
        return len(self.data)

    def getitem(self, index):
        return self.data[index]

    def setitem(self, index, char):
        self.data[index] = char

    def get_raw_address(self):
        return nonmoving_raw_ptr_for_resizable_list(self.data)

    def _get_gc_data(self):
        return get_gc_data_for_list_of_chars(self.data)

    def _get_gc_data_offset(self):
        return get_gc_data_offset_for_list_of_chars()


class StringBuffer(GCBuffer):
    _attrs_ = ['readonly', 'value']
    _immutable_ = True

    def __init__(self, value):
        assert value  is not None
        self.value = value
        self.readonly = 1

    def getlength(self):
        return len(self.value)

    def as_str(self):
        return self.value

    def getitem(self, index):
        return self.value[index]

    def getslice(self, start, stop, step, size):
        if size == 0:
            return ""
        if step == 1:
            assert 0 <= start <= stop
            if start == 0 and stop == len(self.value):
                return self.value
            return self.value[start:stop]
        return Buffer.getslice(self, start, stop, step, size)

    def get_raw_address(self):
        from rpython.rtyper.lltypesystem import rffi
        # may still raise ValueError on some GCs
        return rffi.get_raw_address_of_string(self.value)

    def _get_gc_data_offset(self):
        return (llmemory.offsetof(STR, 'chars') +
                llmemory.itemoffsetof(STR.chars, 0))

    def _get_gc_data(self):
        lls = llstr(self.value)
        return lltype.cast_opaque_ptr(llmemory.GCREF, lls)


class SubBuffer(Buffer):
    _attrs_ = ['buffer', 'offset', 'size', 'readonly']
    _immutable_ = True

    @signature(types.any(), types.instance(Buffer), types.int(), types.int(),
               returns=types.none())
    def __init__(self, buffer, offset, size):
        self.readonly = buffer.readonly
        if isinstance(buffer, SubBuffer):     # don't nest them
            # we want a view (offset, size) over a view
            # (buffer.offset, buffer.size) over buffer.buffer.
            # Note that either '.size' can be -1 to mean 'up to the end'.
            at_most = buffer.getlength() - offset
            if size > at_most or size < 0:
                if at_most < 0:
                    at_most = 0
                size = at_most
            offset += buffer.offset
            buffer = buffer.buffer
        #
        self.buffer = buffer
        self.offset = offset
        self.size = size

    def getlength(self):
        at_most = self.buffer.getlength() - self.offset
        if 0 <= self.size <= at_most:
            return self.size
        elif at_most >= 0:
            return at_most
        else:
            return 0

    def getitem(self, index):
        return self.buffer.getitem(self.offset + index)

    def getslice(self, start, stop, step, size):
        if start == stop:
            return ''     # otherwise, adding self.offset might make them
                          # out of bounds
        return self.buffer.getslice(self.offset + start, self.offset + stop,
                                    step, size)

    def setitem(self, index, char):
        self.buffer.setitem(self.offset + index, char)

    def setslice(self, start, string):
        if len(string) == 0:
            return        # otherwise, adding self.offset might make 'start'
                          # out of bounds
        self.buffer.setslice(self.offset + start, string)

    def get_raw_address(self):
        from rpython.rtyper.lltypesystem import rffi
        ptr = self.buffer.get_raw_address()
        return rffi.ptradd(ptr, self.offset)

    @specialize.ll_and_arg(1)
    def typed_read(self, TP, byte_offset):
        return self.buffer.typed_read(TP, byte_offset + self.offset)

    @specialize.ll_and_arg(1)
    def typed_write(self, TP, byte_offset, value):
        return self.buffer.typed_write(TP, byte_offset + self.offset, value)
