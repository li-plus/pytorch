import functools
import logging

import sympy
import triton

import torch
from .. import config as inductor_config
from ..ir import FixedLayout
from ..lowering import expand, register_lowering
from ..select_algorithm import (
    autotune_select_algorithm,
    ExternKernelChoice,
    realize_inputs,
    TritonTemplate,
)
from ..virtualized import V

log = logging.getLogger(__name__)
aten = torch.ops.aten

# these are the default compute bound choices taken from triton.ops.matmul
mm_configs = [
    triton.Config(
        {"BLOCK_M": 128, "BLOCK_N": 256, "BLOCK_K": 32}, num_stages=3, num_warps=8
    ),
    triton.Config(
        {"BLOCK_M": 256, "BLOCK_N": 128, "BLOCK_K": 32}, num_stages=3, num_warps=8
    ),
    triton.Config(
        {"BLOCK_M": 256, "BLOCK_N": 64, "BLOCK_K": 32}, num_stages=4, num_warps=4
    ),
    # triton.Config(
    #     {"BLOCK_M": 64, "BLOCK_N": 256, "BLOCK_K": 32}, num_stages=4, num_warps=4
    # ),
    triton.Config(
        {"BLOCK_M": 128, "BLOCK_N": 128, "BLOCK_K": 32}, num_stages=4, num_warps=4
    ),
    triton.Config(
        {"BLOCK_M": 128, "BLOCK_N": 64, "BLOCK_K": 32}, num_stages=4, num_warps=4
    ),
    triton.Config(
        {"BLOCK_M": 64, "BLOCK_N": 128, "BLOCK_K": 32}, num_stages=4, num_warps=4
    ),
    triton.Config(
        {"BLOCK_M": 128, "BLOCK_N": 32, "BLOCK_K": 32}, num_stages=4, num_warps=4
    ),
    triton.Config(
        {"BLOCK_M": 64, "BLOCK_N": 32, "BLOCK_K": 32}, num_stages=5, num_warps=2
    ),
    triton.Config(
        {"BLOCK_M": 64, "BLOCK_N": 64, "BLOCK_K": 64}, num_stages=4, num_warps=4
    ),
    triton.Config(
        {"BLOCK_M": 32, "BLOCK_N": 32, "BLOCK_K": 256}, num_stages=2, num_warps=8
    ),
    triton.Config(
        {"BLOCK_M": 64, "BLOCK_N": 64, "BLOCK_K": 16}, num_stages=2, num_warps=4
    ),
    triton.Config(
        {"BLOCK_M": 32, "BLOCK_N": 32, "BLOCK_K": 16}, num_stages=1, num_warps=2
    ),
]


def mm_grid(m, n, meta):
    """
    The CUDA grid size for matmul triton templates.
    """
    return (triton.cdiv(m, meta["BLOCK_M"]) * triton.cdiv(n, meta["BLOCK_N"]), 1, 1)


def acc_type(dtype):
    if dtype in (torch.float16, torch.bfloat16):
        return "tl.float32"
    return f"tl.{dtype}".replace("torch.", "")


@functools.lru_cache(None)
def is_big_gpu(index):
    cores = torch.cuda.get_device_properties(index).multi_processor_count
    if cores < 80:  # V100
        log.warning("not enough cuda cores to use max_autotune mode")
        return False
    return True


def use_triton_template(layout):
    return (
        inductor_config.max_autotune
        and layout.device.type == "cuda"
        and layout.dtype in (torch.float16, torch.bfloat16, torch.float32)
        and is_big_gpu(layout.device.index or 0)
    )


def mm_options(config, sym_k, layout):
    """
    Common options to matmul triton templates.
    """
    even_k_symbolic = (
        # it isn't worth guarding on this
        sympy.gcd(sym_k, config.kwargs["BLOCK_K"])
        == config.kwargs["BLOCK_K"]
    )
    return dict(
        GROUP_M=8,
        EVEN_K=even_k_symbolic,
        ALLOW_TF32=torch.backends.cuda.matmul.allow_tf32,
        ACC_TYPE=acc_type(layout.dtype),
        num_stages=config.num_stages,
        num_warps=config.num_warps,
        **config.kwargs,
    )


aten_mm = ExternKernelChoice(torch.mm)

mm_template = TritonTemplate(
    name="mm",
    grid=mm_grid,
    source=r"""
{{def_kernel("A", "B")}}
    M = {{size("A", 0)}}
    N = {{size("B", 1)}}
    K = {{size("A", 1)}}
    stride_am = {{stride("A", 0)}}
    stride_ak = {{stride("A", 1)}}
    stride_bk = {{stride("B", 0)}}
    stride_bn = {{stride("B", 1)}}

    # based on triton.ops.matmul
    pid = tl.program_id(0)
    grid_m = (M + BLOCK_M - 1) // BLOCK_M
    grid_n = (N + BLOCK_N - 1) // BLOCK_N

    # re-order program ID for better L2 performance
    width = GROUP_M * grid_n
    group_id = pid // width
    group_size = min(grid_m - group_id * GROUP_M, GROUP_M)
    pid_m = group_id * GROUP_M + (pid % group_size)
    pid_n = (pid % width) // (group_size)

    rm = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    rn = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
    ram = tl.max_contiguous(tl.multiple_of(rm % M, BLOCK_M), BLOCK_M)
    rbn = tl.max_contiguous(tl.multiple_of(rn % N, BLOCK_N), BLOCK_N)
    rk = tl.arange(0, BLOCK_K)
    A = A + (ram[:, None] * stride_am + rk[None, :] * stride_ak)
    B = B + (rk[:, None] * stride_bk + rbn[None, :] * stride_bn)

    acc = tl.zeros((BLOCK_M, BLOCK_N), dtype=ACC_TYPE)
    for k in range(K, 0, -BLOCK_K):
        if EVEN_K:
            a = tl.load(A)
            b = tl.load(B)
        else:
            a = tl.load(A, mask=rk[None, :] < k, other=0.)
            b = tl.load(B, mask=rk[:, None] < k, other=0.)
        acc += tl.dot(a, b, allow_tf32=ALLOW_TF32)
        A += BLOCK_K * stride_ak
        B += BLOCK_K * stride_bk

    # rematerialize rm and rn to save registers
    rm = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    rn = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
    idx_m = rm[:, None]
    idx_n = rn[None, :]
    mask = (idx_m < M) & (idx_n < N)

    # inductor generates a suffix
    {{store_output(("idx_m", "idx_n"), "acc", "mask")}}
""",
)


def mm_args(mat1, mat2, *others, layout=None):
    """
    Common arg processing for mm,bmm,addmm,etc
    """
    mat1, mat2 = realize_inputs(mat1, mat2)
    *b1, m, k1 = mat1.get_size()
    *b2, k2, n = mat2.get_size()
    b = [V.graph.sizevars.guard_equals(a, b) for a, b in zip(b1, b2)]
    k = V.graph.sizevars.guard_equals(k1, k2)
    if layout is None:
        layout = FixedLayout(
            mat1.get_device(),
            mat1.get_dtype(),
            [*b, m, n],
        )

    others = [realize_inputs(expand(x, layout.size)) for x in others]

    return [m, n, k, layout, mat1, mat2, *others]


@register_lowering(aten.mm)
def tuned_mm(mat1, mat2, *, layout=None):
    m, n, k, layout, mat1, mat2 = mm_args(mat1, mat2, layout=layout)

    # options to tune from
    choices = [aten_mm.bind((mat1, mat2), layout)]
    if use_triton_template(layout):
        for config in mm_configs:
            choices.append(
                mm_template.generate(
                    (mat1, mat2),
                    layout,
                    **mm_options(config, k, layout),
                )
            )

    return autotune_select_algorithm(choices, [mat1, mat2], layout)


def addmm_epilogue(dtype, alpha, beta):
    def epilogue(acc, bias):
        if alpha != 1:
            acc = V.ops.mul(acc, V.ops.constant(alpha, dtype))
        if beta != 1:
            bias = V.ops.mul(bias, V.ops.constant(beta, dtype))
        return V.ops.add(acc, bias)

    return epilogue


aten_addmm = ExternKernelChoice(torch.addmm)


@register_lowering(aten.addmm)
def tuned_addmm(inp, mat1, mat2, *, alpha=1, beta=1, layout=None):
    m, n, k, layout, mat1, mat2, inp = mm_args(mat1, mat2, inp, layout=layout)

    # options to tune from
    choices = [aten_addmm.bind((inp, mat1, mat2), layout, alpha=alpha, beta=beta)]
    if use_triton_template(layout):
        for config in mm_configs:
            choices.append(
                mm_template.generate(
                    (inp, mat1, mat2),
                    layout,
                    **mm_options(config, k, layout),
                    prefix_args=1,
                    epilogue_fn=addmm_epilogue(layout.dtype, alpha, beta),
                )
            )

    return autotune_select_algorithm(choices, [inp, mat1, mat2], layout)
