"""Entry point for running as: python -m ymir.agents.golang_rebuild"""

import asyncio

from ymir.agents.golang_rebuild.workflow import main

if __name__ == "__main__":
    asyncio.run(main())
