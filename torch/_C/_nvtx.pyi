# Defined in torch/csrc/cuda/shared/nvtx.cpp
def rangePushA(message: str) -> int: ...
def rangePop() -> int: ...
def rangeStartA(message: str) -> int: ...
def rangeEnd(int) -> None: ...
def markA(message: str) -> None: ...
