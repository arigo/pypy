import sys
from itertools import izip
from collections import OrderedDict

from rpython.translator.tool.cbuild import ExternalCompilationInfo
from rpython.rlib.rfile import FILEP
from rpython.rtyper.lltypesystem import rffi, lltype
from rpython.rtyper.tool import rfficache, rffi_platform
from rpython.flowspace.model import Constant, const
from rpython.flowspace.specialcase import register_flow_sc
from rpython.flowspace.flowcontext import FlowingError

from . import cmodel as model
from ._cparser import Parser


CNAME_TO_LLTYPE = {
    'char': rffi.CHAR,
    'double': rffi.DOUBLE, 'long double': rffi.LONGDOUBLE,
    'float': rffi.FLOAT, 'FILE': FILEP.TO}

def add_inttypes():
    for name in rffi.TYPES:
        if name.startswith('unsigned'):
            rname = 'u' + name[9:]
        else:
            rname = name
        rname = rname.replace(' ', '').upper()
        CNAME_TO_LLTYPE[name] = rfficache.platform.types[rname]

add_inttypes()
CNAME_TO_LLTYPE['int'] = rffi.INT_real
CNAME_TO_LLTYPE['wchar_t'] = lltype.UniChar
if 'ssize_t' not in CNAME_TO_LLTYPE:  # on Windows
    CNAME_TO_LLTYPE['ssize_t'] = CNAME_TO_LLTYPE['long']

def cname_to_lltype(name):
    return CNAME_TO_LLTYPE[name]

class DelayedStruct(object):
    def __init__(self, name, fields, TYPE):
        self.struct_name = name
        self.type_name = None
        self.fields = fields
        self.TYPE = TYPE

    def get_type_name(self):
        if self.type_name is not None:
            return self.type_name
        elif not self.struct_name.startswith('$'):
            return 'struct %s' % self.struct_name
        else:
            raise ValueError('Anonymous struct')

    def __repr__(self):
        return "<struct {struct_name}>".format(**vars(self))


class CTypeSpace(object):
    def __init__(self, parser=None, definitions=None, macros=None,
                 headers=None, includes=None):
        self.definitions = definitions if definitions is not None else {}
        self.macros = macros if macros is not None else {}
        self.structs = {}
        self.ctx = parser if parser else Parser()
        self.headers = headers if headers is not None else ['sys/types.h']
        self.parsed_headers = []
        self.sources = []
        self._config_entries = OrderedDict()
        self.includes = []
        self.struct_typedefs = {}
        self._handled = set()
        self._frozen = False
        self._cdecl_type_cache = {} # {cdecl: TYPE} cache
        if includes is not None:
            for header in includes:
                self.include(header)

    def include(self, other):
        self.ctx.include(other.ctx)
        self.structs.update(other.structs)
        self.includes.append(other)

    def parse_source(self, source):
        self.sources.append(source)
        self.ctx.parse(source)
        self.configure_types()

    def parse_header(self, header_path):
        self.headers.append(str(header_path))
        self.parsed_headers.append(header_path)
        self.ctx.parse(header_path.read())
        self.configure_types()

    def add_typedef(self, name, obj, quals):
        assert name not in self.definitions
        tp = self.convert_type(obj, quals)
        if isinstance(tp, DelayedStruct):
            if tp.type_name is None:
                tp.type_name = name
            tp = self.realize_struct(tp)
            self.structs[obj.realtype] = tp
        self.definitions[name] = tp

    def add_macro(self, name, value):
        assert name not in self.macros
        self.macros[name] = value

    def new_struct(self, obj):
        if obj.name == '_IO_FILE':  # cffi weirdness
            return cname_to_lltype('FILE')
        struct = DelayedStruct(obj.name, None, lltype.ForwardReference())
        # Cache it early, to avoid infinite recursion
        self.structs[obj] = struct
        if obj.fldtypes is not None:
            struct.fields = zip(
                obj.fldnames,
                [self.convert_field(field) for field in obj.fldtypes])
        return struct

    def convert_field(self, obj):
        tp = self.convert_type(obj)
        if isinstance(tp, DelayedStruct):
            tp = tp.TYPE
        return tp

    def realize_struct(self, struct):
        type_name = struct.get_type_name()
        entry = rffi_platform.Struct(type_name, struct.fields)
        self._config_entries[entry] = struct.TYPE
        return struct.TYPE

    def build_eci(self):
        all_sources = []
        for cts in self.includes:
            all_sources.extend(cts.sources)
        all_sources.extend(self.sources)
        all_headers = self.headers
        for x in self.includes:
            for hdr in x.headers:
                if hdr not in all_headers:
                    all_headers.append(hdr)
        if sys.platform == 'win32':
            compile_extra = ['-Dssize_t=long']
        else:
            compile_extra = []
        return ExternalCompilationInfo(
            post_include_bits=all_sources, includes=all_headers,
            compile_extra=compile_extra)

    def configure_types(self):
        for name, (obj, quals) in self.ctx._declarations.iteritems():
            if obj in self.ctx._included_declarations:
                continue
            if name in self._handled:
                continue
            self._handled.add(name)
            if name.startswith('typedef '):
                name = name[8:]
                self.add_typedef(name, obj, quals)
            elif name.startswith('macro '):
                name = name[6:]
                self.add_macro(name, obj)
        if not self._config_entries:
            return
        eci = self.build_eci()
        result = rffi_platform.configure_entries(list(self._config_entries), eci)
        for entry, TYPE in izip(self._config_entries, result):
            # hack: prevent the source from being pasted into common_header.h
            del TYPE._hints['eci']
            self._config_entries[entry].become(TYPE)
        self._config_entries.clear()

    def convert_type(self, obj, quals=0):
        if isinstance(obj, model.DefinedType):
            return self.convert_type(obj.realtype, obj.quals)
        if isinstance(obj, model.PrimitiveType):
            return cname_to_lltype(obj.name)
        elif isinstance(obj, model.StructType):
            if obj in self.structs:
                return self.structs[obj]
            return self.new_struct(obj)
        elif isinstance(obj, model.PointerType):
            TO = self.convert_type(obj.totype)
            if TO is lltype.Void:
                return rffi.VOIDP
            elif isinstance(TO, DelayedStruct):
                TO = TO.TYPE
            if isinstance(TO, lltype.ContainerType):
                return lltype.Ptr(TO)
            else:
                if obj.quals & model.Q_CONST:
                    return lltype.Ptr(lltype.Array(
                        TO, hints={'nolength': True, 'render_as_const': True}))
                else:
                    return rffi.CArrayPtr(TO)
        elif isinstance(obj, model.FunctionPtrType):
            if obj.ellipsis:
                raise NotImplementedError
            args = [self.convert_type(arg) for arg in obj.args]
            res = self.convert_type(obj.result)
            return lltype.Ptr(lltype.FuncType(args, res))
        elif isinstance(obj, model.VoidType):
            return lltype.Void
        elif isinstance(obj, model.ArrayType):
            return rffi.CFixedArray(self.convert_type(obj.item), obj.length)
        else:
            raise NotImplementedError

    def gettype(self, cdecl):
        try:
            return self._cdecl_type_cache[cdecl]
        except KeyError:
            result = self._real_gettype(cdecl)
            self._cdecl_type_cache[cdecl] = result
            return result

    def _real_gettype(self, cdecl):
        obj = self.ctx.parse_type(cdecl)
        result = self.convert_type(obj)
        if isinstance(result, DelayedStruct):
            result = result.TYPE
        return result

    def cast(self, cdecl, value):
        return rffi.cast(self.gettype(cdecl), value)

    def parse_func(self, cdecl):
        cdecl = cdecl.strip()
        if cdecl[-1] != ';':
            cdecl += ';'
        ast, _, _ = self.ctx._parse(cdecl)
        decl = ast.ext[-1]
        tp, quals = self.ctx._get_type_and_quals(decl.type, name=decl.name)
        return FunctionDeclaration(decl.name, tp)

    def _freeze_(self):
        if self._frozen:
            return True

        @register_flow_sc(self.cast)
        def sc_cast(ctx, v_decl, v_arg):
            if not isinstance(v_decl, Constant):
                raise FlowingError(
                    "The first argument of cts.cast() must be a constant.")
            TP = self.gettype(v_decl.value)
            return ctx.appcall(rffi.cast, const(TP), v_arg)

        @register_flow_sc(self.gettype)
        def sc_gettype(ctx, v_decl):
            if not isinstance(v_decl, Constant):
                raise FlowingError(
                    "The argument of cts.gettype() must be a constant.")
            return const(self.gettype(v_decl.value))

        self._frozen = True
        return True

class FunctionDeclaration(object):
    def __init__(self, name, tp):
        self.name = name
        self.tp = tp

    def get_llargs(self, cts):
        return [cts.convert_type(arg) for arg in self.tp.args]

    def get_llresult(self, cts):
        return cts.convert_type(self.tp.result)

def parse_source(source, includes=None, headers=None, configure_now=True):
    cts = CTypeSpace(headers=headers, includes=includes)
    cts.parse_source(source)
    return cts
