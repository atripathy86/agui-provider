"""Ensure AG-UI dependencies are available from persistent volume."""
import subprocess
import sys
import logging
from pathlib import Path
from helpers.extension import Extension
from agent import LoopData

logger = logging.getLogger("agui-provider")

# Persistent lib dir on the a0-usr volume — survives container rebuilds
_USR_LIB = Path("/a0/usr/lib")
_CHECKED = False


class AGUIDeps(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        global _CHECKED
        if _CHECKED:
            return
        _CHECKED = True

        # Ensure usr/lib is on sys.path so imports find volume-installed packages
        lib_str = str(_USR_LIB)
        if lib_str not in sys.path:
            sys.path.insert(0, lib_str)

        # Quick check: can we import aiohttp?
        try:
            import aiohttp  # noqa: F401
            logger.debug("[deps] aiohttp available")
            return
        except ImportError:
            pass

        # Install aiohttp to persistent volume
        _USR_LIB.mkdir(parents=True, exist_ok=True)
        logger.info(f"[deps] aiohttp missing — installing to {_USR_LIB}")
        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "pip", "install",
                    "-q", "--target", lib_str,
                    "aiohttp>=3.9",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                logger.info("[deps] aiohttp installed to persistent volume")
            else:
                logger.error(f"[deps] pip install failed: {result.stderr}")
        except Exception as e:
            logger.error(f"[deps] failed to install: {e}")
