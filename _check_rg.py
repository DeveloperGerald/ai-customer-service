from langchain_core.runnables import RunnableGenerator
import inspect
print("RunnableGenerator.__init__:")
sig = inspect.signature(RunnableGenerator.__init__)
for name, p in list(sig.parameters.items())[1:]:
    ann = p.annotation
    ann_str = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann)[:60])
    def_str = "<required>" if p.default is inspect.Parameter.empty else repr(p.default)
    print(f"  {name} : {ann_str} = {def_str}")
print()
print("MRO:", [c.__name__ for c in RunnableGenerator.__mro__[:10]])
print()
# 尝试在对象属性上设置 tags（不通过 __init__）
def dummy():
    import types
    yield "a"
import asyncio
async def dummy_a():
    yield "x"
rg = RunnableGenerator(dummy_a)
print("rg tags default:", rg.tags if hasattr(rg, "tags") else "NO .tags attr")
try:
    rg2 = RunnableGenerator(dummy_a)
    print("before set tags, type(rg2.tags) =", type(rg2.tags) if hasattr(rg2, "tags") else None)
    rg2.tags = ["chat_model"]
    print("after set tags:", rg2.tags)
except Exception as e:
    print("FAIL direct attr set:", type(e).__name__, e)
try:
    from langchain_core.runnables import RunnableConfigurable
    print("hasattr with_config:", hasattr(rg, "with_config"))
    if hasattr(rg, "with_config"):
        rgw = rg.with_config(tags=["chat_model"])
        print("with_config(tags=[...]) OK, type:", type(rgw).__name__)
        print(" .tags after with_config:", getattr(rgw, "tags", None))
except Exception as e:
    print("FAIL with_config:", type(e).__name__, e)
print()
# 最终目的：让 RunnableGenerator 携带 tags=["chat_model"] 透传到 astream_events 分发
# 实际验证：runnable.astream 的 tags 透传
try:
    rg3 = RunnableGenerator(dummy_a)
    rg3.tags = ["chat_model", "foo"]
    print("Direct assignment OK. rg3.tags =", rg3.tags)
except Exception as e:
    print("Direct assignment FAIL:", e)
