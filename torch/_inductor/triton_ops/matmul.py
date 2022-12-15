import torch

from ..utils import has_triton

print("000AJSKDLJS")
if has_triton():

    import triton
    import triton.language as tl

    from .autotune import mm_autotune, mm_heuristics

    @mm_autotune(get_io_bound_configs=True)
    @mm_heuristics()
    @triton.jit
    def _kernel(
        A,
        B,
        C,
        M,
        N,
        K,
        stride_am,
        stride_ak,
        stride_bk,
        stride_bn,
        stride_cm,
        stride_cn,
        allow_tf32: tl.constexpr,
        use_dot: tl.constexpr,
        BLOCK_M: tl.constexpr,
        BLOCK_N: tl.constexpr,
        BLOCK_K: tl.constexpr,
        GROUP_M: tl.constexpr,
        SPLIT_K: tl.constexpr,
        EVEN_K: tl.constexpr,
        ACC_TYPE: tl.constexpr,
    ):
        # matrix multiplication
        pid = tl.program_id(0)
        pid_z = tl.program_id(1)
        grid_m = (M + BLOCK_M - 1) // BLOCK_M
        grid_n = (N + BLOCK_N - 1) // BLOCK_N
        # re-order program ID for better L2 performance
        width = GROUP_M * grid_n
        group_id = pid // width
        group_size = min(grid_m - group_id * GROUP_M, GROUP_M)
        pid_m = group_id * GROUP_M + (pid % group_size)
        pid_n = (pid % width) // (group_size)
        # do matrix multiplication
        rm = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
        rn = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
        ram = tl.max_contiguous(tl.multiple_of(rm % M, BLOCK_M), BLOCK_M)
        rbn = tl.max_contiguous(tl.multiple_of(rn % N, BLOCK_N), BLOCK_N)
        rk = pid_z * BLOCK_K + tl.arange(0, BLOCK_K)
        # pointers
        A = A + (ram[:, None] * stride_am + rk[None, :] * stride_ak)
        B = B + (rk[:, None] * stride_bk + rbn[None, :] * stride_bn)
        acc = tl.zeros((BLOCK_M, BLOCK_N), dtype=ACC_TYPE)
        for k in range(K, 0, -BLOCK_K * SPLIT_K):
            if EVEN_K:
                a = tl.load(A)
                b = tl.load(B)
            else:
                a = tl.load(A, mask=rk[None, :] < k, other=0.0)
                b = tl.load(B, mask=rk[:, None] < k, other=0.0)
            if use_dot:
                acc += tl.dot(a, b, allow_tf32=allow_tf32)
            else:
                for i in range(0, BLOCK_M):
                    for j in range(0, BLOCK_N):
                        pass
            A += BLOCK_K * SPLIT_K * stride_ak
            B += BLOCK_K * SPLIT_K * stride_bk
        acc = acc.to(C.dtype.element_ty)
        # rematerialize rm and rn to save registers
        rm = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
        rn = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
        C = C + (rm[:, None] * stride_cm + rn[None, :] * stride_cn)
        mask = (rm < M)[:, None] & (rn < N)[None, :]
        # handles write-back with reduction-splitting
        if SPLIT_K == 1:
            tl.store(C, acc, mask=mask)
        else:
            tl.atomic_add(C, acc, mask=mask)

    def _triton_addmm_gpu(self: torch.Tensor, a: torch.Tensor, b: torch.Tensor, beta: float, alpha: float, c: torch.Tensor, allow_tf32) -> torch.Tensor:
        assert beta == 0
        assert alpha == 1
        # handle non-contiguous inputs if necessary
        if a.stride(0) > 1 and a.stride(1) > 1:
            a = a.contiguous()
        if b.stride(0) > 1 and b.stride(1) > 1:
            b = b.contiguous()
        # checks constraints
        assert a.shape[1] == b.shape[0], "incompatible dimensions"
        M, K = a.shape
        _, N = b.shape

        # accumulator types
        ACC_TYPE = (
            tl.float32
            if a.dtype in [torch.float16, torch.bfloat16, torch.float32]
            else tl.int32
        )

        # launch kernel (grid defined as using def instead of lambda to pass `make lint`)
        def grid(META):
            return (
                triton.cdiv(M, META["BLOCK_M"]) * triton.cdiv(N, META["BLOCK_N"]),
                META["SPLIT_K"],
            )

        # grid = lambda META: (
        #     triton.cdiv(M, META["BLOCK_M"]) * triton.cdiv(N, META["BLOCK_N"]),
        #     META["SPLIT_K"],
        # )
        use_dot = False
        _kernel[grid](
            a,
            b,
            c,
            M,
            N,
            K,
            a.stride(0),
            a.stride(1),
            b.stride(0),
            b.stride(1),
            c.stride(0),
            c.stride(1),
            allow_tf32=allow_tf32,
            use_dot=use_dot,
            GROUP_M=8,
            ACC_TYPE=ACC_TYPE,
        )

        return c

    print("AJSKDLJS")
    aten_lib = torch.library.Library("aten", "IMPL")
    aten_lib.impl("aten::_triton_addmm", _triton_addmm_gpu, "CUDA")

    class _matmul_out:
        kernel = _kernel

        @staticmethod
        def _call(a, b, out, allow_tf32=True):
            _triton_addmm_gpu(out, out, a, b, 0, 1, allow_tf32);

        @staticmethod
        def forward(a, b, out, allow_tf32=True):
            return _matmul_out._call(a, b, out, allow_tf32)

    matmul_out = _matmul_out.forward
