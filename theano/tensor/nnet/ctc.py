from __future__ import (division, absolute_import, print_function)
import os
import os.path
import theano.tensor as T
from theano import config
from theano import gof
from theano.gof import local_optimizer
from theano.gof.cmodule import GCC_compiler
from theano.tensor.opt import register_canonicalize
from theano.tensor.opt import register_stabilize
from theano.tensor.extra_ops import cpu_contiguous
from theano.gradient import grad_undefined


def _ctc_find_lib():
    """
    Find the directory that contains libwarpctc.so
    """
    lib_found = False
    for lib_dir in ["build", "lib", "lib64"]:
        lib_path = os.path.join(config.ctc.root, lib_dir)
        if os.path.isdir(lib_path) and os.path.exists(lib_path):
            lib_found = os.path.exists(os.path.join(lib_path, "libwarpctc.so"))
            if lib_found:
                return True, lib_path
    return False, None

def _ctc_check_compile(ctc_lib_path):
    preambule = """
#include <string.h>
#include "ctc.h"
"""

    body = """
ctcOptions options;
memset(&options, 0, sizeof(ctcOptions));
options.loc = CTC_CPU;
options.num_threads = 1;
"""

    params = ["-I%s" % (os.path.join(config.ctc.root, "include"))]
    params.extend(['-I%s' % (os.path.dirname(__file__))])
    params.extend(["-L%s" % (ctc_lib_path)])
    params.extend(["-l", "warpctc"])
    compiler_res = GCC_compiler.try_flags(
        params, preambule=preambule, body=body,
        try_run=False, output=True)

    avail, out, err = compiler_res if isinstance(compiler_res, tuple) else (compiler_res, None, None)
    if not avail:
        return False, ("cannot compile with warp-ctc. "
                       "We got this error:\n" + str(err))
    return True, None

def ctc_present():
    if ctc_present.avail is not None:
        return ctc_present.avail
    ctc_present.avail, ctc_lib_path = _ctc_find_lib()
    if ctc_lib_path is None:
        ctc_present.msg = 'libwarpctc.so could not be found. ',
        'Please check your config.ctc.root variable.'
    else:
        ctc_present.path = ctc_lib_path
        ctc_present.avail, ctc_present.msg = _ctc_check_compile(ctc_present.path)
    return ctc_present.avail

ctc_present.avail = None
ctc_present.msg = None
ctc_present.path = None

def ctc_available():
    if config.ctc.root == '':
        ctc_available.msg = 'ctc.root variable is not set, please set it ',
        'to the root directory of the CTC library in ',
        'your system.'
        return False
    elif os.name == 'nt':
        ctc_available.msg = 'Windows platforms are currently not supported ',
        'by underlying CTC library (warp-ctc).'
        return False
    elif not ctc_present():
        ctc_available.msg = ctc_present.msg
        return False

    ctc_available.path = ctc_present.path
    return True

ctc_available.msg = None
ctc_available.path = None


class ConnectionistTemporalClassification(gof.COp, gof.OpenMPOp):
    """
    CTC loss function wrapper.

    Notes
    -----
    Using the wrapper requires that Baidu's warp-ctc library is installed and the
    configuration variables `config.ctc.enabled` and `config.ctc.root` be properly
    set.

    Parameters
    ----------
    compute_grad
        If set to True, enables the computation of gradients of the CTC loss function.
    """
    __props__ = ('compute_grad', 'default_output',)

    _cop_num_inputs = 3
    _cop_num_outputs = 2

    func_file = "./ctc_wrapper.c"
    func_name = "APPLY_SPECIFIC(ctc_cost_cpu)"

    def __init__(self, compute_grad=True):
        if not ctc_available():
            raise RuntimeError('Baidu CTC is not available and '
                               'ConnectionistTemporalClassification Op '
                               'can not be constructed.')

        gof.COp.__init__(self, self.func_file, self.func_name)
        gof.OpenMPOp.__init__(self)

        self.compute_grad = compute_grad
        # Return only the cost. Gradient will be returned by grad()
        self.default_output = 0

    def c_lib_dirs(self):
        assert ctc_available.path is not None
        return [ctc_available.path]

    def c_libraries(self):
        return ["warpctc"]

    def c_header_dirs(self):
        # We assume here that the header is available at the include directory
        # of the CTC root directory.
        return [os.path.join(config.ctc.root, "include")]

    def c_compile_args(self):
        return gof.OpenMPOp.c_compile_args(self)

    def c_headers(self):
        return ["ctc.h"] + gof.OpenMPOp.c_headers(self)

    def make_node(self, activations, labels, input_lengths):
        t_activations = T.as_tensor_variable(activations)
        # Ensure activations array is C-contiguous
        t_activations = cpu_contiguous(t_activations)

        t_labels = T.as_tensor_variable(labels)
        t_input_lengths = T.as_tensor_variable(input_lengths)

        if t_activations.type.dtype != 'float32':
            raise TypeError('Activations must use the float32 type!')

        if t_labels.type.dtype != 'int32':
            raise TypeError('Labels must use the int32 type!')

        if t_input_lengths.type.dtype != 'int32':
            raise TypeError('Label lengths must use the int32 type!')

        costs = T.fvector(name="ctc_cost")
        outputs = [costs]
        if self.compute_grad:
            self.gradients = T.ftensor3(name="ctc_grad")
            outputs += [self.gradients]

        return gof.Apply(self, inputs=[t_activations, t_labels, t_input_lengths],
                         outputs=outputs)

    def L_op(self, inputs, outputs, output_grads):
        gradients = self.gradients
        assert gradients is not None

        grad_op = output_grads[0]
        total_grad = T.basic.batched_dot(grad_op, gradients.dimshuffle(1, 0, 2)).dimshuffle(1, 0, 2)
        return [total_grad,
                grad_undefined(self, 1, inputs[1]),
                grad_undefined(self, 2, inputs[2])]


def ctc(activations, labels, input_lengths):
    """
    Compute CTC loss function.

    Notes
    -----
    Using the loss function requires that Baidu's warp-ctc library is installed and the
    configuration variables `config.ctc.enabled` and `config.ctc.root` be properly set.

    Parameters
    ----------
    activations
        Three-dimensional tensor, which has a shape of (t, m, p), where
        t is the time index, m is the minibatch index, and p is the index
        over the probabilities of each symbol in the alphabet. The memory
        layout is assumed to be in C-order, which consists in the slowest
        to the fastest changing dimension, from left to right. In this case,
        p is the fastest changing dimension.
    labels
        A 1-D tensor of all the labels for the minibatch.
    input_lengths
        A 1-D tensor with the number of time steps for each sequence in
        the minibatch.

    Returns
    -------
    1-D array
        Cost of each example in the minibatch.
    """
    return ConnectionistTemporalClassification()(activations, labels, input_lengths)


# Disable gradient computation if not needed
@register_canonicalize
@register_stabilize
@local_optimizer([ConnectionistTemporalClassification])
def local_ctc_no_grad(node):
    if isinstance(node.op, ConnectionistTemporalClassification):
        if len(node.outputs) > 1:
            if len(node.outputs[1].clients) == 0:   # gradient is not used
                return [ConnectionistTemporalClassification(compute_grad=False)(*node.inputs), None]
    return False
