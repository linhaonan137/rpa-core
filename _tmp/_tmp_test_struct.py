"""临时实测：flow_add_node / flow_move_node / flow_drop_node（用完即删）"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.mcp import (flow_save, flow_read, flow_add_node, flow_drop_node,
                     flow_move_node, flow_delete)

N = "_tmp_struct_test"

def mk(n=3):
    return {"name": "struct", "nodes": [{"id": f"n{i}", "type": "noop", "args": {}} for i in range(1, n + 1)],
            "edges": [{"id": f"e{i}", "from": f"n{i}", "to": f"n{i+1}"} for i in range(1, n)]}

async def show(tag):
    r = await flow_read(N)
    if not r.get("ok"):
        print(f"  {tag} 读取失败:", r.get("error")); return
    nodes = [n["id"] for n in r["flow"]["nodes"]]
    edges = [f'{e["from"]}→{e["to"]}' for e in r["flow"]["edges"]]
    print(f"  {tag}: 节点={nodes}  边={edges}")

async def main():
    await flow_delete(N, purge_snapshots=True)
    r = await flow_save(N, mk(3))
    print("0 初始:", r.get("ok")); await show("  ")

    print("\n1 flow_add_node(after=n2)")
    r = await flow_add_node(N, after="n2", type="sleep", args={"ms": 50})
    print("  dry_run:", r.get("ok"), r.get("would_add") or r.get("node") or r.get("error"), "| changes:", r.get("changes"))
    r = await flow_add_node(N, after="n2", type="sleep", args={"ms": 50}, reason="测插入", dry_run=False)
    print("  实跑:", r.get("ok"), "新节点:", r.get("node") or r.get("id"), "| 错误:", r.get("error"))
    new_id = r.get("node") or r.get("id")
    await show("  ")

    if new_id:
        print(f"\n2 flow_move_node(node={new_id}, after=n1)")
        r = await flow_move_node(N, node=new_id, after="n1", reason="测移动", dry_run=False)
        print("  实跑:", r.get("ok"), "| changes:", r.get("changes"), "| 错误:", r.get("error"))
        await show("  ")

        print(f"\n3 flow_drop_node(node={new_id})")
        r = await flow_drop_node(N, node=new_id, reason="测删除", dry_run=False)
        print("  实跑:", r.get("ok"), "| changes:", r.get("changes"), "| 错误:", r.get("error"))
        await show("  ")

    await flow_delete(N, purge_snapshots=True)
    print("\n清理完成，文件还在吗:", (Path.cwd() / "flows" / f"{N}.json").exists())

asyncio.run(main())
