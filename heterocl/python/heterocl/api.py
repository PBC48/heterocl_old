from . import tensor
from . import util
from . import types
from .code_builder import CodeBuilder
from .resizer import Resizer, Downsizer
from tvm.api import _IterVar, decl_buffer, convert
from tvm.build_module import build as _build
from tvm.ndarray import array, cpu
from tvm import var as _var
from tvm import schedule as _schedule
from tvm import placeholder as _placeholder
from tvm import _api_internal
from tvm import make as _make
from tvm import expr as _expr
from tvm import stmt as _stmt
import inspect
import ast
import numbers

def convert_dtype(dtype):
  if isinstance(dtype, types.Type):
    if isinstance(dtype, types.Int):
      bits = dtype.bits
      if bits is None:
        return "int32"
      elif isinstance(bits, numbers.Number):
        return "int" + str(bits)
      elif isinstance(bits, (tuple, list)):
        return "int" + str(max(bits))
      else:
        raise ValueError("Unkown integer")
    elif isinstance(dtype, types.UInt):
      bits = dtype.bits
      if bits is None:
        return "uint32"
      elif isinstance(bits, numbers.Number):
        return "uint" + str(bits)
      elif isinstance(bits, (tuple, list)):
        return "uint" + str(max(bits))
      else:
        raise ValueError("Unkown integer")
    elif isinstance(dtype, types.Fixed):
      bits = dtype.bits
      fracs = dtype.fracs
      assert not bits is None, "Must provide bits for a fixed point"
      if fracs is None:
        return "int" + str(bits)
      else:
        assert fracs <= bits, "Fractional part cannot be greater than total bits"
        return "fixed" + str(bits) + "_" + str(fracs)
    elif isinstance(dtype, types.UFixed):
      bits = dtype.bits
      fracs = dtype.fracs
      assert not bits is None, "Must provide bits for a fixed point"
      if fracs is None:
        return "uint" + str(bits)
      else:
        assert fracs <= bits, "Fractional part cannot be greater than total bits"
        return "ufixed" + str(bits) + "_" + str(fracs)

    else:
      raise NotImplementedError()
  else:
    return dtype


def var(name = "var", dtype = "int32"):
  dtype = convert_dtype(dtype)
  return tensor.Var(_var(name = name, dtype = dtype))

def placeholder(shape, name = "placeholder", dtype = "int32"):
  dtype = convert_dtype(dtype)
  builder = CodeBuilder.current
  p = tensor.Tensor(shape, dtype, name)
  op = tensor.Operation(None, p, None)
  tensor.Operation.op_list.append(op)
  if len(builder) == 0:
    return p
  else:
    builder[-1].emit(lambda x: _make.Allocate(p.buf.data, dtype, shape, util.true(), x))
    return p

def local(init = 0, name = "local", dtype = "int32"):
  dtype = convert_dtype(dtype)
  builder = CodeBuilder.current
  assert len(builder) != 0, "hcl.local must be used inside a code builder"
  p = tensor.Tensor((1,), dtype, name)
  builder[-1].emit(lambda x: _make.Allocate(p.buf.data, dtype, (1,), util.true(), x))
  p[0] = init
  op = tensor.Operation(None, p, None)
  tensor.Operation.op_list.append(op)
  return p

# TODO: record the index of all calls and loops
def compute(shape, inputs, fcompute, name = "compute", dtype = "int32"):
  code = fcompute.__code__
  args = code.co_varnames
  nargs = code.co_argcount
  dtype = convert_dtype(dtype)

  #assert (len(shape) == nargs), "fcompute does not match output dimension"

  indices = [_IterVar((0, shape[n]), args[n], 0) for n in range(0, nargs)]
  var_list = [i.var for i in indices]
  body = None
  p = tensor.Tensor(shape, dtype, name)

  cb_count = len(CodeBuilder.stmt_stack)

  ret = fcompute(*var_list)

  index, _, _ = util.get_index(shape, indices, 0)
  if isinstance(ret, tensor.TensorSlice):
    ret = ret.asnode()
    body = _make.Store(p.buf.data, _make.Cast(dtype, ret), index)
  elif isinstance(ret, tensor.Tensor):
    var = _var("comp_var")
    extent = ret.shape[0]
    body = _make.For(var, 0, extent, 0, 0,
        _make.Store(p.buf.data, _make.Cast(dtype, ret[var]), index * extent + var))
  elif isinstance(ret, (_expr.Expr, numbers.Number)):
    body = _make.Store(p.buf.data, _make.Cast(dtype, ret), index)
  else:
    raise ValueError("Unrecognized return value in hcl.compute")
  if len(CodeBuilder.stmt_stack) == cb_count:
    body = util.make_for(indices, body, 0)
  else:
    body = _make.Block(CodeBuilder.get(), body)
    body = util.make_for(indices, body, 0)

  builders = CodeBuilder.current
  if len(builders) != 0:
    builder = builders[-1]
    builder.emit(lambda x: _make.Allocate(p.buf.data, dtype, shape, util.true(), x))
    builder.emit(body)

  op = tensor.Operation(inputs, p, body)
  tensor.Operation.op_list.append(op)

  return p

#TODO: incorrect
def update(tensor, inputs, fcompute, name = "update", extern = []):
  code = fcompute.__code__
  args = code.co_varnames

  # collect input placeholders
  input_tensors = []
  input_vars = []
  for i in inputs:
    input_tensors.append(i) if isinstance(i, tvm.tensor.Tensor) else input_vars.append(i)
  input_placeholders = [decl_buffer(i.shape, i.dtype, i.op.name) for i in input_tensors]
  update_placeholder = decl_buffer(tensor.shape, tensor.dtype, tensor.op.name)
  if tensor not in inputs:
    input_tensors.append(tensor)
    input_placeholders.append(update_placeholder)
  # infer output dtype
  output_placeholders = [decl_buffer((1,), "int32", name)]
  # collect body
  lambda_root = visitor.LambdaVisitor().enter(inspect.getsource(code)) # extract the lambda function AST
  body = visitor.HalideIRVisitor().compile_lambda(lambda_root, input_tensors, input_placeholders, input_vars, update_placeholder, extern) # compile Python AST to Halide IR
  op = _tvm_api._ExternOp(name, "", input_tensors, input_placeholders, output_placeholders, body)
  op = op.output(0)

  return op

#TODO: incorrect
def block(fblock, inputs, args = [], name = "block", extern = []):
  input_placeholders = [decl_buffer(i.shape, i.dtype, i.op.name) for i in inputs]
  output_placeholders = [decl_buffer((1,), "int32", name)]
  # compile fblock to Halide IR
  if len(args) == 0:
    args = inputs
  body = visitor.HalideIRVisitor().compile_block(fblock, inputs, input_placeholders, args, extern)
  op = _tvm_api._ExternOp(name, "", inputs, input_placeholders, output_placeholders, body)
  op = op.output(0)

  return op

def mut_compute(shape, inputs, fcompute, name = "mut_compute", dtype = "int32"):
  code = fcompute.__code__
  args = code.co_varnames
  nargs = code.co_argcount
  dtype = convert_dtype(dtype)
  p = tensor.Tensor((1,), "int32", name)

  assert (len(shape) == nargs), "fcompute does not match output dimension"

  indices = [_IterVar((0, shape[n]), args[n], 0) for n in range(0, nargs)]
  var_list = [i.var for i in indices]

  fcompute(*var_list)
  assert len(CodeBuilder.stmt_stack) != 0
  ret = CodeBuilder.get()
  body = util.make_for(indices, ret, 0)

  op = tensor.Operation(inputs, p, body)
  tensor.Operation.op_list.append(op)

  return p

def resize(inputs, dtype):
  from_vars = []
  to_vars = []
  assert isinstance(dtype, (str, types.Type)), "Wrong input to resize data type"
  dtype = convert_dtype(dtype)
  if not isinstance(inputs, (list, tuple)):
    inputs = [inputs]
  for i in inputs:
    if isinstance(i, tensor.Var):
      from_vars.append(i.var)
      new_var = _var(i.name, dtype)
      i.var = new_var
      to_vars.append(new_var)
    else:
      from_vars.append(i.buf.data)
      new_buf = decl_buffer(i.shape, dtype, i.name)
      i.buf = new_buf
      i.dtype = dtype
      to_vars.append(new_buf.data)
  op_list = tensor.Operation.op_list
  assert len(op_list) > 0, "Resize must be used before create_schedule!!"
  bodies = Resizer(from_vars, to_vars, dtype).enter(op_list)
  for i in range(len(op_list)):
    op_list[i].body = bodies[i]
  builders = CodeBuilder.current
  if len(builders) > 0:
    Resizer(from_vars, to_vars, dtype).enter_cb(CodeBuilder)

def downsize(inputs, dt_var):
  from_vars = []
  if not isinstance(inputs, (list, tuple)):
    inputs = [inputs]
  for i in inputs:
    if isinstance(i, tensor.Var):
      from_vars.append(i.var)
    else:
      from_vars.append(i.buf.data)
  op_list = tensor.Operation.op_list
  assert len(op_list) > 0, "Downsize must be used before create_schedule!!"
  bodies = Downsizer(from_vars, dt_var.var).enter(op_list)
  for i in range(len(op_list)):
    op_list[i].body = bodies[i]

def create_schedule(t):
  for op in tensor.Operation.op_list:
    if op.inputs is None: #placeholder
      p = op.output
      p.tensor = _api_internal._Placeholder(p.buf.shape, p.dtype, p.name)
    else:
      i = op.inputs
      p = op.output
      for _i in i:
        assert not _i.tensor is None
      i_tensor = [_i.tensor for _i in i]
      i_buf = [_i.buf for _i in i]
      o_buf = [p.buf]
      p.tensor = _api_internal._ExternOp(p.name, "", i_tensor, i_buf, o_buf, op.body).output(0)

  tensor.Operation.op_list = []
  return _schedule.create_schedule(t.op)

def build(schedule, inputs):
  new_inputs = []
  for i in inputs:
    if isinstance(i, tensor.Tensor):
      new_inputs.append(i.tensor)
    else:
      new_inputs.append(i.var)

  return _build(schedule, new_inputs)

def reduce_axis(dom, name = "ra"):
  return _IterVar(dom, name, 2)

def comm_reducer(init, freduce, dtype = "int32"):

  def make_reduce(expr, axis, where = True):
    with CodeBuilder() as cb:
      if isinstance(init, (_expr.Expr, numbers.Number)):
        out = local(init, "reducer", dtype)
        with cb._for_itervar(axis):
          with cb._if(where):
            out[0] = freduce(expr, out[0])
        return out[0]
      else: # a list or tensor
        shape = init.shape
        assert len(shape) == 1, "Wrong init value for reducer!!"
        out = compute(shape, [], lambda x: init[x], name = "out", dtype = init.dtype)
        with cb._for_itervar(axis):
          with cb._if(where):
            ret = freduce(expr, out)
            cb.emit(CodeBuilder.get())
            with cb._for(0, shape[0]) as i:
              out[i] = ret[i]
        return out

  return make_reduce

def asarray(arr, dtype = "int32", ctx = cpu(0)):
  dtype = convert_dtype(dtype)
  return array(arr, dtype, ctx)

sum = comm_reducer(0, lambda x, y: x + y)