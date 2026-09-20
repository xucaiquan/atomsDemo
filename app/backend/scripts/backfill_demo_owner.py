"""一次性迁移：把 owner_key 为 NULL 的历史项目标为只读演示项目（S1.3）。

判据统一为 ``is_demo``（不用 owner_key IS NULL 做运行期判据，见设计文档 §8 偏离 4）。
脚本幂等，可重复执行：

    cd app/backend && python scripts/backfill_demo_owner.py

未执行脚本的后果：那些项目对所有身份不可见（fail-closed 安全侧），不是人人可见；
list_projects 的自检会打 ERROR 日志提示。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from core.database import db_manager  # noqa: E402


async def main() -> None:
    async with db_manager.session() as session:
        result = await session.execute(
            text("UPDATE projects SET is_demo = true WHERE owner_key IS NULL")
        )
        await session.commit()
        print(f"回填完成，影响行数: {result.rowcount}")


if __name__ == "__main__":
    asyncio.run(main())
