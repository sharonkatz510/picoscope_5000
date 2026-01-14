from __future__ import annotations

from typing import Tuple, Literal
import os
import numpy as np


def read_acq_bin(path: str, dtype: Literal["float16", "float32"] = "float16") -> Tuple[np.ndarray, np.ndarray]:
    """
    Read a raw acquisition .bin file produced by the recording feature
    in picoscope_5000_block.

    Format:
        - float16 samples in volts (default since Jan 2026)
            Prior versions used float32; pass dtype="float32" to read older files.
    - Channel A samples first, then Channel B samples
    - No header; file names like "acq_001.bin"

    Parameters
    ----------
    path : str
        Path to the .bin file

    Returns
    -------
    (a, b) : Tuple[np.ndarray, np.ndarray]
        Two 1-D arrays of dtype matching `dtype` with Channel A and Channel B samples.

    Raises
    ------
    FileNotFoundError
        If the provided path does not exist.
    ValueError
        If the file size is not compatible with the expected layout
        (i.e., number of float32 values is not even).
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No such file: {path}")

    dt = np.float16 if dtype == "float16" else np.float32
    data = np.fromfile(path, dtype=dt)
    if data.size % 2 != 0:
        raise ValueError(
            f"Invalid file size: expected an even number of float32 values, got {data.size}"
        )
    n = data.size // 2
    a = data[:n].copy()
    b = data[n:].copy()
    return a, b
