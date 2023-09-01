# Copyright 2023 Nod Labs, Inc
# Portions Copyright 2022 The IREE Authors
#
# Licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

from contextlib import contextmanager
import threading
from typing import Any, Callable, List, Sequence

from iree.compiler.ir import (
    FunctionType,
    InsertionPoint,
    Location,
    Operation,
    SymbolTable,
    TypeAttr,
    Value,
)

from iree.compiler.dialects import (
    func as func_d,
)

from torch.utils._pytree import (
    tree_map,
    tree_flatten,
    tree_unflatten,
)

from numpy import number
from collections.abc import Mapping

from .builder import ModuleBuilder

_thread_state = threading.local()


class Intrinsic:
    """Objects which interact natively with the tracing system implement this."""

    def resolve_ir_values(self, proc_trace: "ProcedureTrace") -> Sequence[Value]:
        raise NotImplementedError(
            f"Cannot use {self} as an expression in a procedural function"
        )

    def resolve_call(self, proc_trace: "ProcedureTrace", *args, **kwargs):
        raise NotImplementedError(
            f"Cannot use {self} as the target of a call in a procedural function"
        )


class CallableIntrinsic(Intrinsic):
    """Intrinsic subclass that supports calls.

    This is separate so as to make error handling better (i.e. does not support
    calls) for intrinsics that are not callable.
    """

    def __call__(self, *args, **kwargs):
        return current_ir_trace().handle_call(self, args, kwargs)


class IrTrace:
    """Gets callbacks for tracing events."""

    def finalize(self):
        """Called when the trace is finished (popped off the stack)."""
        pass

    def handle_call(self, target: Intrinsic, args, kwargs):
        raise NotImplementedError(f"The current trace scope does not support calls")


class ImmediateIrTrace(IrTrace):
    ...


def _trace_scopes() -> List[IrTrace]:
    try:
        trace_scopes = _thread_state.trace_scopes
    except AttributeError:
        trace_scopes = _thread_state.trace_scopes = [ImmediateIrTrace()]
    return trace_scopes


@contextmanager
def new_ir_trace_scope(ir_trace: IrTrace):
    trace_scopes = _trace_scopes()
    trace_scopes.append(ir_trace)
    try:
        yield ir_trace
    finally:
        ir_trace.finalize()
        del trace_scopes[-1]


def current_ir_trace() -> IrTrace:
    return _trace_scopes()[-1]


class ProcedureTrace(IrTrace):
    """Captures execution of a Python func into IR."""

    def __init__(self, *, module_builder: ModuleBuilder, func_op: func_d.FuncOp):
        self.module_builder = module_builder
        self.func_op = func_op
        self.context = func_op.context
        self.ip = InsertionPoint(self.func_op.entry_block)
        self.return_types = None
        self.loc = self.func_op.location

    @staticmethod
    def define_func(
        module_builder: ModuleBuilder,
        *,
        symbol_name: str,
        arguments: Sequence,
        loc: Location,
    ) -> "ProcedureTrace":
        # Unpack arguments.
        arguments_flat, arguments_tree_def = tree_flatten(arguments)
        argument_ir_types = []
        # TODO: Transform to meta types and populate argument_ir_types.

        # TODO: Make public when has def.
        with loc:
            _, func_op = module_builder.create_func_op(symbol_name, argument_ir_types)
        return ProcedureTrace(module_builder=module_builder, func_op=func_op)

    def trace_py_func(self, py_f: Callable):
        with new_ir_trace_scope(self) as t:
            # TODO: Create IR proxies for python arguments.
            argument_py_values = []
            return_py_value = py_f(*argument_py_values)
            if return_py_value is None:
                self.emit_return()
            else:
                # TODO: Materialize python values as IR.
                return_ir_values = []
                self.emit_return(*return_ir_values)

    def emit_return(self, *ir_values: Sequence[Value]):
        with self.loc, self.ip:
            func_d.ReturnOp(ir_values)
            # Check or rewrite the function return type.
            value_types = [v.type for v in ir_values]
            if self.return_types:
                if value_types != self.return_types:
                    raise ValueError(
                        f"Multi-return function must return same types. "
                        f"{value_types} vs {self.return_types}"
                    )
                return
            self.return_types = value_types
            ftype = self.func_op.type
            ftype = FunctionType.get(ftype.inputs, value_types)
            self.func_op.attributes["function_type"] = TypeAttr.get(ftype)
            assert self.func_op.verify(), "Created function is invalid"

    def handle_call(self, target: Intrinsic, args, kwargs):
        """Implements calls to jittable functions."""
        with self.loc, self.ip:
            return target.resolve_call(self, *args, **kwargs)