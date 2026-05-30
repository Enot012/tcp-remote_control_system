"""
Точка запуска TCP-сервера.
"""

import asyncio
import traceback

from managers import Logger, ServerState
from server import TCPServer


if __name__ == "__main__":
    try:
        asyncio.run(TCPServer())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        Logger.crash(e, traceback.format_exc(), ServerState())
