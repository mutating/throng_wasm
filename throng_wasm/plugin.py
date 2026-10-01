from pathlib import Path
from typing import List, Optional, Union

from throng import throng

from throng_wasm.manager import WasmManager


@throng.plugin
def wasm(path: Union[str, Path] = '.', exclude: Optional[List[str]] = None) -> WasmManager:
    return WasmManager(path, exclude=exclude)
