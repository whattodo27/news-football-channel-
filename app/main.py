"""Compatibility entrypoint for the container."""
import asyncio
from newsbot.main import main
if __name__ == "__main__": asyncio.run(main())
